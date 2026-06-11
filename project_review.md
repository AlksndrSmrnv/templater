# Анализ проекта Template Maker — рекомендации по улучшению

> Фокус: фичи и удобство (UX/DX). Безопасность не рассматривалась.

---

## 🔴 Топ-10: что даст максимальный эффект

| # | Рекомендация | Почему важно |
|---|--------------|--------------|
| 1 | **GIN-индексы на JSONB-поля** (`attributes`, `placeholders`, `llm_meta`) | Сейчас поиск клиента по ИНН или шаблона по плейсхолдеру — Full Table Scan. С ростом данных UI «встанет». |
| 2 | **Autocomplete вместо `list_all()` в форме заполнения шаблона** | Сейчас на страницу «Заполнить» грузятся **все** клиенты/счета/карты. При 1000+ сущностях страница неработоспособна. |
| 3 | **Пагинация + offset в `FilledTemplateRepository`** | Есть `limit=200`, но нет `offset`. Пользователь никогда не увидит историю заполнений старше 200-й записи. |
| 4 | **Dry-run импорта с preview** | Перед реальным импортом JSON-пакета показывать, что создастся, что обновится, что пропустится — без записи в БД. |
| 5 | **Persistent `LLMService` в `lifespan` + кэширование анализа** | Сейчас LLM-клиент создаётся на каждый запрос: лишние TCP-handshake, base64-decode, temp-файлы. Кэш по `hash(content)` даст мгновенный re-process. |
| 6 | **JSON-mode для GigaChat (`response_format={"type":"json_object"}`)** | Уберёт 90% хрупкого парсинга (fences, trailing commas, prose) в `LLMService`. |
| 7 | **Фоновая batch-обработка коллекций LLM с прогрессом** | Сейчас 50 шаблонов обрабатываются синхронно по одному — минуты ожидания без индикации. Нужен job-статус + polling/SSE. |
| 8 | **Клонирование шаблонов и сущностей** | «Дублировать» → копия с суффиксом `(копия)`. Ускоряет работу оператора в разы. |
| 9 | **Версионирование `MessageTemplate`** | Таблица `template_versions` + UI «История». Ошибочная перезапись шаблона станет обратимой. |
| 10 | **Группировка `filled_templates` в сессии (пакеты)** | 8 шаблонов под одну сделку сейчас болтаются отдельно. `FillSession` даст вкладку «Пакеты» с единым экспортом. |

---

## 🗄 База данных и модели

### Высокий
- **GIN-индексы** на `clients.attributes`, `accounts.attributes`, `cards.attributes`, `message_templates.placeholders` (и др. JSONB, по которым идёт поиск).
- **Нормализовать роли в `FilledTemplate`**. Заменить 9 nullable FK-колонок на отдельную таблицу `filled_template_roles(role_type, client_id, account_id, card_id, display_label)`. Добавление новой роли = INSERT, не DDL-миграция.
- **Вынести seed-данные `attribute_definitions` из миграций** в idempotent-скрипт (`scripts/seed_default_attributes.py` с `ON CONFLICT DO UPDATE`). Сейчас чтобы поменять label дефолтного атрибута — нужна миграция.
- **Конфигурация пула БД** в `.env`: `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_RECYCLE`, `DB_ECHO`. Передавать в `create_async_engine`.

### Средний
- **Soft-delete** (`deleted_at`) для `Client`, `Account`, `Card`, `MessageTemplate`, `Collection`. Восстановление из корзины вместо необратимого удаления.
- **`external_id` / `source_collection_id`** у `Collection` и сущностей. Повторный импорт той же Postman-коллекции должен обновлять существующую, а не плодить дубликаты.
- **Naming convention в `Base.metadata`** Alembic — для предсказуемых имён constraint и чистых autogenerate-миграций.
- **`AppSetting` с мета-информацией**: добавить `label`, `data_type`, `default_value`, `description`, `category`. UI настроек сможет авто-рендерить формы без фронтенд-хардкода.

### Низкий
- **Generated columns** для часто ищущихся JSONB-полей (например, `attributes->>'inn'`) — B-tree index на computed column.
- **`draft` / `published` статус** у `MessageTemplate`. Черновики не попадают в bulk-заполнение.

---

## 📦 Репозитории и Pydantic-схемы

### Высокий
- **`FilledTemplateRepository`: добавить `offset`** в `list_all()` и `list_by_template()`. Иначе история просто обрезается.
- **`TemplateRepository.delete_by_collection()`**: заменить `SELECT + цикл DELETE` на единый `DELETE FROM … WHERE collection_id = :id`. При 500 шаблонах в коллекции разница колоссальная.
- **Строгая валидация в `schemas/attribute.py`**: `data_type` → `Literal["string","int","number","bool","date","datetime","text","enum"]`, `name` → `pattern=r"^[a-zA-Z0-9_]+$"`, `entity_type` → `Literal["client","account","card"]`. Плохие имена ломают JSONB-ключи и Jinja2-плейсхолдеры.
- **`TemplateFillRequest` валидация связей**: `model_validator`, который проверяет, что `sender_account_id` принадлежит `sender_client_id` (и аналогично для других ролей).
- **Out/Response схемы** для всех сущностей с `id`, `created_at`, `updated_at`. Сейчас фронтенд и OpenAPI не знают точную структуру ответов.

### Средний
- **`update()` метод в репозиториях**. Сейчас `Attribute`, `Collection`, `FilledTemplate`, `Template` не имеют его — сервисы мутируют объекты напрямую.
- **`add_many()` / `delete_many()`** в `TemplateRepository` и `AttributeRepository`. Ускорит импорт коллекций и bulk-операции.
- **Поиск (`ilike`) и пагинация** в `CollectionRepository`, `AttributeRepository`, `TemplateRepository`. При росте данных `list_all` станет неюзабельным.
- **SettingsRepository: in-memory кэш** с TTL 60 сек. Настройки читаются часто, а сейчас каждый `get()` — запрос в БД.
- **`count_attribute_usage()`**: заменить Python substring-поиск по всем шаблонам на SQL `content LIKE '%attr_name%'` или `tsvector`. Сейчас загружаются **все** `content` в память.

---

## ⚙️ Бизнес-логика (сервисы)

### Высокий
- **Smart-diff при обновлении `content` шаблона**. Если изменение — только форматирование (pretty-print vs minified), не сбрасывать placeholders автоматически. Сравнивать нормализованные JSON/XML деревья.
- **Batch-удаление сущностей с каскадом и preview**. Кнопка «Удалить клиента и всё под ним» с модальным окном: «Будут удалены 3 счёта и 12 карт. Подтвердить?».
- **Конкурентная обработка коллекции LLM**. `asyncio.gather` / `Semaphore` (лимит 3–5 параллельных) вместо последовательного цикла.
- **Кэширование `build_field_catalog`** в `TemplateService` (TTL 60 сек + инвалидация при изменении `AttributeDefinition`). Сейчас строится на лету при каждом LLM-вызове.

### Средний
- **Сессии/пакеты заполнения (`FillSession`)**. Группировать `filled_templates` под одним `session_id`. В UI — отдельная вкладка «Пакеты» с bulk-экспортом ZIP.
- **Конфигурируемый шаблон автоимени** для `filled_template`. В `AppSetting` вынести Jinja2-шаблон имени (по умолчанию текущий формат). Пользователь настроит: `{{ date }} — {{ template_name }} ({{ sender }})`.
- **Partial / selective export**. Флаги `include_entities`, `include_templates`, `include_schema`. Выгрузить только workspace без клиентов, или только схему атрибутов для миграции между стендами.
- **Архивация вместо hard delete** для `Collection` и `MessageTemplate`. «Показать архив» в дереве.
- **Pre-filter в `TransferAssistant`**. Перед отправкой каталога клиентов в LLM отфильтровать по запросу (top-20 релевантных). Сейчас **все** сущности грузятся в промпт — при 100 клиентах контекст обрезается.
- **Стабилизировать short-ID** (`T1`, `C1`, `A1`) в `TransferAssistant`. Сортировать по `created_at` перед нумерацией, иначе ID плавают между вызовами.

### Низкий
- **Preview заполнения без сохранения**. Эндпоинт, который возвращает отрендеренный текст и список `unresolved`, но не создаёт запись в БД — для live-режима отладки.
- **Merge (patch) policy для импорта**. Обновлять только явно переданные поля; `tags` дополнять, а не затирать.

---

## 📥 Импорт / Экспорт и коллекции

### Высокий
- **Интерполяция Postman-переменных `{{var}}`**. Сейчас они остаются «сырыми» в шаблонах. Нужно подставлять значения из секции `variable` коллекции или превращать в плейсхолдеры.
- **Сохранение query-параметров** при разборе URL Postman. Сейчас `url.query` теряется.
- **`warnings` в `ParsedRequest`**. Malformed-ноды и пропущенные части (formdata, GraphQL, unsupported auth) должны сообщаться пользователю, а не игнорироваться.
- **Учёт `Content-Type` заголовка** при определении формата тела — надёжнее, чем content-sniff.

### Средний
- **Поддержка GraphQL-тел** (`body.mode == "graphql"`) — извлекать `query` + `variables`.
- **Извлечение `request.auth` в плейсхолдеры**. Basic / Bearer / API-key → заголовок с плейсхолдером (`{{token}}`).
- **`original_id` / `original_uid` из Postman** в `ParsedRequest` — для будущего re-import / merge без дублей.
- **Абстрактный базовый класс `BaseImporter`** + `ImporterRegistry`. Добавление Insomnia / OpenAPI сведётся к одному методу `parse()`.

### Низкий
- **Импорт сущностей из CSV/Excel**. Маппинг колонок на `attribute_definitions` → preview → import. Критично для операционистов.
- **Обратный экспорт в Postman v2.1**. Собрать JSON из `Collection` + `MessageTemplate` обратно.

---

## 🤖 LLM-интеграция

### Высокий
- **Persistent `LLMService` через FastAPI `lifespan`**. Создавать один раз, хранить в `app.state`. Убрать ad-hoc `async with llm_service()` в каждом роуте.
- **Кэширование `analyze_template`** по `hash(content + fmt + catalog_version)`. LRU / TTL-cache. Повторный reprocess = мгновенный ответ.
- **JSON-mode для GigaChat**. Передавать `response_format={"type":"json_object"}` и удалить эвристический `_parse_json`.
- **Graceful degradation (`NullLLMClient`)**. При `llm_active=False` не 500, а пустой результат + флаг `llm_skipped=True`. UI переключается в ручной режим.

### Средний
- **Параллельный вызов meta + mapping** в `analyze_template` (через `asyncio.gather`) с опциональным флагом — уменьшит latency вдвое.
- **Token-budget / truncation в `PromptBuilder`**. Если промпт превышает лимит контекста, усечь `leaf.value` и `catalog` с `log.warning`.
- **Метрики в `ChatResponse`**: `latency_ms`, `model`, `finish_reason`, `request_id`. Пробрасывать в `debug`-панель UI.
- **Jinja2-шаблоны для промптов** в `app/templates/llm/*.j2`. Версионирование без redeploy.
- **Few-shot из базы**. Подтягивать 2–3 успешные разметки из `filled_templates` как динамические примеры в промпт.
- **Проверка срока сертификата GigaChat** при старте приложения. «Сертификат протухает через 3 дня» — понятнее, чем SSL-ошибка в runtime.

### Низкий
- **Streaming** (`chat_stream`) для длинных ответов — через SSE/HTMX.
- **Именованные пулы `LLMCoordinator`**: `heavy` (analyze, concurrency=2) и `light` (pick, concurrency=5).
- **Circuit breaker** после N ошибок подряд.

---

## 🔌 API и роуты

### Высокий
- **Autocomplete эндпоинты** для клиентов/счетов/карт с `ILIKE` по `attributes`, `description`, `tags` на уровне БД. Заменить `list_all()` в `page_fill`.
- **Фоновая обработка коллекций LLM**. Таблица `collection_jobs` (id, status, processed, total, errors). `process_collection_llm` ставит задачу, возвращает `job_id`. Фронт polling'ом обновляет прогресс-бар.
- **Спиннер + `disabled`-статус** на всех LLM-кнопках (`hx-indicator`, `hx-disabled-elt`). Сейчас пользователь может кликнуть повторно, не понимая, что идёт запрос.
- **Пагинация и фильтры в `filled_templates.py`**. `offset`, фильтры по `template_id`, дате, клиенту.

### Средний
- **Разделить монолит `templates_reg.py`** (935 строк) на 3–4 модуля: `workspace`, `editor`, `fill`.
- **Drag-and-drop reorder и move** шаблонов в коллекции (SortableJS + HTMX).
- **Bulk-move / bulk-delete** шаблонов в коллекции. Чекбоксы + тулбар «Переместить в…».
- **Redirect с `/` на `/templater/`** (307). Сейчас корень возвращает 404.
- **Дашборд на `home.html`**. Карточки: количество клиентов/счетов/карт/шаблонов/заполненных шаблонов. График «Заполненные шаблоны по дням». Последние 5 снапшотов.
- **Preview импорта** (`POST /import-htmx/preview`). Таблица: что создастся (зелёное), что обновится (жёлтое), что пропустится — без записи в БД.

### Низкий
- **Nested transactions (savepoints)** в `uow.py` для комплексных операций (move-request с reorder).
- **Breadcrumb-навигация** в шаблонах: `Главная > Клиенты > Иванов И.И.`.

---

## 🎨 Frontend и UI/UX

### Высокий
- **Сохранение состояния фильтров и сортировки в URL**. После F5 или возврата из drawer фильтры не должны сбрасываться (`history.replaceState` + чтение при загрузке).
- **Bulk-операции в списке сущностей**. Чекбоксы + «С выбранными: Удалить / Назначить тег / Экспорт». Shift+Click для range-выделения.
- **Защита от потери данных** (`beforeunload` + автосохранение черновика в `localStorage` для форм сущностей и редактора шаблонов).
- **Quick-edit сущностей прямо в drawer**. Переключатель «Редактировать» внутри `entity_detail.html` без перехода на отдельную страницу.
- **Drag-and-drop зона для импорта JSON** (визуальная зона с прогрессом, как в `upload.html`).

### Средний
- **Presets (профили) для заполнения шаблонов**. Сохранять частые комбинации sender/receiver в `localStorage` и подставлять одним кликом.
- **Live-preview при заполнении**. Чекбокс «Обновлять предпросмотр автоматически» с debounce 400 мс → `hx-post` на `/fill/render`.
- **Autocomplete тегов** при редактировании сущности (`hx-get` существующих тегов по префиксу).
- **Keyboard-навигация в редакторе плейсхолдеров**: `Esc` — закрыть dropdown, `↑/↓` — ходить по списку, `Enter` — выбрать.
- **Fuzzy-поиск в dropdown плейсхолдеров** (не только `includes`, но и приблизительное совпадение).
- **Expand/Collapse all + фильтр по HTTP-методу** в дереве коллекций.
- **Side-by-side diff** в просмотре `filled_template`: оригинал vs заполненный с подсветкой изменений.
- **Кнопка «Копировать в буфер»** рядом с каждым значением в drawer и для всего шаблона.
- **Тёмная тема** (`data-theme` + переключатель в настройках, сохранение в `localStorage`).

### Низкий
- **Избранное / Недавние** в дереве шаблонов (звёздочка + секция вверху).
- **Skeleton-заглушки** вместо «Загрузка…» при открытии drawer.
- **Print-CSS**: скрыть sidebar/header, показать только `filled_content`.
- **Undo-тост** вместо `hx-confirm`: «Удалено · Отменить» в течение 5 сек (требует soft-delete на бэкенде).

---

## 🧪 Тесты и Developer Experience

### Высокий
- **Общие `tests/stubs.py` и `tests/factories.py`**. `FakeSession`, `FakeFormRequest`, `FakeTemplateRenderer`, `TemplateContextFactory` — убрать дублирование в 15+ тестах.
- **Фикстура `client` с `dependency_overrides`** в `conftest.py`. 80% тестов сейчас вручную патчат сессии.
- **LLM: concurrency, retry, cache** — тесты на семафор, backoff, cache hit.
- **Settings factory**. Сделать `get_settings()` переопределяемым через FastAPI `Depends`, а не глобальный singleton с `lru_cache`.

### Средний
- **pytest-cov + pytest-xdist** в `pyproject.toml`. `addopts = "-n auto --cov=app --cov-report=term-missing"`.
- **Correlation ID в логах**. Middleware `X-Request-ID` + structlog binding.
- **Snapshot-тестирование промптов** (syrupy). Перенести тексты в `.j2`-файлы, тестировать через snapshot — уйдёт от хрупких match'ей на русские строки.
- **CLI для `run_local.sh`**: аргументы `--no-migrate`, `--port`.
- **justfile / Makefile**: `just test`, `just lint`, `just migrate`, `just run`.
- **Docker healthcheck** для приложения (`curl /templater/health`).

### Низкий
- **Hypothesis / property-based тесты** для `walker.py` round-trip и `paths.py`.
- **CLI-утилиты для отладки**: `python -m app.utils.walker inspect file.json`, `python -m app.utils.paths inspect "/a/b~1c"`.
- **Pre-commit hooks** (ruff, mypy).
- **pgAdmin в docker-compose** (закомментированный сервис для удобства разработки).
