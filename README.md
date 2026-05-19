# Template Maker

Веб-приложение для подготовки тестовых данных и шаблонов сообщений (JSON/XML) для систем, тестируемых по REST.

## Возможности

- CRUD клиентов, счетов и карт с проверкой ссылочной целостности.
- Универсальные справочники: валюты, типы счетов, типы карт, банки, гражданство.
- Расширяемая схема атрибутов (новые поля добавляются без миграций; устаревшие помечаются, а не удаляются).
- Реестр шаблонов сообщений с подсветкой синтаксиса и интерактивным редактором плейсхолдеров.
- Анализ шаблонов GigaChat'ом: предложения соответствий полей и метаинформация о шаблоне.
- Заполнение шаблонов отправителем и получателем (`{{sender.*}}` / `{{receiver.*}}`).
- Селективный экспорт и импорт с автодобавлением зависимостей.
- Заготовка под отправку сообщений по REST (UI-stub, без функционала).

## Архитектура

| Слой | Решение |
|---|---|
| Backend | Python 3.11, FastAPI (async) |
| ORM | SQLAlchemy 2.0 (async) + Alembic |
| БД | PostgreSQL 16 (JSONB) |
| Frontend | Jinja2 SSR + vanilla JS + Fetch API, embedded CSS |
| LLM | GigaChat (через единый клиент в `app/llm/`) |
| Контейнеризация | Docker + docker-compose |
| CI | Jenkinsfile |

Бизнес-данные клиентов, счетов и карт хранятся в гибридной схеме:
структурные колонки (FK, теги, описание) + `attributes JSONB`. Описание атрибутов
живёт в таблице `attribute_definitions`, что позволяет добавлять новые поля без
изменения структуры таблиц и без поломки уже заведённых данных.

## Локальный запуск (Docker)

```bash
cp .env.example .env
docker-compose up --build
```

После старта приложение доступно по `http://localhost:8000`. Контейнер сам:
1. Дожидается готовности Postgres,
2. Применяет миграции Alembic,
3. Засеивает базовые справочники (валюты, типы и т. д.).

## Локальный запуск (без Docker)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # отредактировать DATABASE_URL

# поднять Postgres локально (например, через docker)
docker run -d --name tm-pg -e POSTGRES_USER=template_maker -e POSTGRES_PASSWORD=template_maker -e POSTGRES_DB=template_maker -p 5432:5432 postgres:16-alpine

alembic upgrade head
python -m scripts.seed_reference_data
uvicorn app.main:app --reload
```

## Подключение GigaChat

Заполни переменные окружения в `.env`:

```
GIGACHAT_BASE_URL=https://gigachat.example/api/v1
GIGACHAT_CERT_B64=<base64 содержимого client.pem>
GIGACHAT_KEY_B64=<base64 содержимого client.key>
GIGACHAT_MODEL=GigaChat-3-Ultra
```

Без этих значений LLM-функции автоматически отключаются — анализ шаблонов
использует эвристический матчинг полей по их именам.

## Раскладка кода

```
app/
├── main.py                # FastAPI factory
├── config.py              # настройки (pydantic-settings)
├── db/                    # модели + async session factory
├── repositories/          # доступ к БД
├── services/              # бизнес-логика (целостность, схема, шаблоны, LLM-стадии)
├── schemas/               # Pydantic DTO
├── routes/                # FastAPI роутеры (HTML + JSON)
├── llm/                   # GigaChat client, prompt builder, LLM service
├── utils/                 # walkers, ошибки
├── templates/             # Jinja2
└── static/                # CSS, JS
alembic/                   # миграции
scripts/                   # CLI-утилиты (сидинг)
tests/                     # pytest
docker/                    # Dockerfile + entrypoint
```

## Расширение схемы атрибутов

Любой атрибут добавляется через UI «Настройки → Атрибуты сущностей» либо вручную
вставкой строки в `attribute_definitions`. Поддерживаемые типы:
`string`, `text`, `int`, `number`, `bool`, `date`, `datetime`, `enum`, `ref`.

Атрибуты типа `ref` ссылаются на справочник через `options.ref_entity` (имя из
`currency` / `account_type` / `card_type` / `bank` / `citizenship`). Целостность
ссылок проверяется на уровне сервиса при сохранении сущности и при удалении
справочного значения.

Атрибут **нельзя удалить**, только пометить как `is_deprecated=true` — он
скрывается из форм и таблиц, но значения в существующих записях остаются.

## Тесты

```bash
pytest -q
```

Тестируется бизнес-логика без зависимости от Postgres: walker JSON/XML, рендер
шаблонов в HTML, эвристическое и LLM-сопоставление полей, обработка ошибок LLM,
работа с сертификатами.

## CI

`Jenkinsfile` поднимает venv, прогоняет ruff и pytest, собирает Docker-образ и
пушит его в реестр (используется секрет `docker-registry`).

## Дорожная карта

- Отправка сообщений в тестируемую систему по REST (вкладка «Отправка сообщений»).
- Сборка пакета шаблон + клиенты по запросу на естественном языке через GigaChat.
- Импорт с предпросмотром изменений и rollback на ошибки.
