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

### Схема БД

Приложение живёт в выделенной PostgreSQL-схеме — имя задаётся переменной
`DB_SCHEMA` (по умолчанию `templater`; должно быть простым идентификатором:
строчные латинские буквы, цифры и `_`). Схема прописывается в `search_path`
соединения и создаётся автоматически при первой миграции, так что все таблицы
изолированы в ней и не пересекаются с другими данными базы. Чтобы использовать
стандартную схему, задайте `DB_SCHEMA=public`.

## Локальный запуск (без Docker)

Приложение запускается локальным процессом, но подключается к **настоящим**
PostgreSQL и GigaChat — адреса и креды берутся из `.env`. Локальный Postgres
поднимать не нужно.

Для локального окружения используется `uv`; Docker и Jenkins по-прежнему читают
сгенерированный `requirements.txt`.

Логи по умолчанию пишутся в JSON (`LOG_JSON=true`). Для читаемого локального
вывода можно поставить `LOG_JSON=false`; уровень задаётся через `LOG_LEVEL`.

**1. Заполнить `.env` реальными значениями:**

```bash
cp .env.example .env
```

В `.env` указать:
- `DATABASE_URL` — адрес и креды вашего PostgreSQL.
  Пароль со спецсимволами percent-encode (`@`→`%40`, `$`→`%24`, `|`→`%7C`).
- `DB_SCHEMA` — выделенная схема (по умолчанию `templater`, создаётся автоматически).
- `GIGACHAT_BASE_URL`, `GIGACHAT_CERT_B64`, `GIGACHAT_KEY_B64` — для GigaChat.
  Сертификат и ключ кодируются в base64:
  ```bash
  base64 -i client.pem | tr -d '\n'   # → GIGACHAT_CERT_B64
  base64 -i client.key | tr -d '\n'   # → GIGACHAT_KEY_B64
  ```
  Если оставить пустыми — LLM-функции отключатся, остальное приложение работает.

**2. Запустить одной командой:**

```bash
./scripts/run_local.sh
```

Скрипт создаёт виртуальное окружение, ставит зависимости, применяет миграции
Alembic, засеивает базовые справочники и поднимает `uvicorn` на
`http://127.0.0.1:8000`.

**Вручную** (то же самое по шагам):

```bash
uv sync --frozen --all-extras
uv run alembic upgrade head
uv run python -m scripts.seed_reference_data
uv run uvicorn app.main:app --reload
```

## Подключение GigaChat

LLM включается, когда заданы все три: `GIGACHAT_BASE_URL`, `GIGACHAT_CERT_B64`,
`GIGACHAT_KEY_B64` (см. выше про base64). `GIGACHAT_MODEL` по умолчанию
`GigaChat-3-Ultra`. Параметры таймаутов/ретраев — `LLM_*` в `.env`.

Без этих значений LLM-функции автоматически отключаются — анализ шаблонов
использует эвристический матчинг полей по их именам.

`GIGACHAT_CERT_B64` и `GIGACHAT_KEY_B64` должны содержать base64 именно от
PEM-файлов. Быстрая проверка:

```bash
echo "$GIGACHAT_CERT_B64" | base64 -d | head -c 30
```

Корректный сертификат начинается с `-----BEGIN CERTIFICATE-----`. Если виден
мусор или команда падает, переменная содержит не PEM в base64.

Маркер `-----BEGIN` проверяет только контейнер. Если приложение всё равно
получает ошибку OpenSSL (`[SSL] PEM lib`), сохраните пару во временные файлы и
проверьте, что ключ читается без пароля и соответствует сертификату:

```bash
printf '%s' "$GIGACHAT_CERT_B64" | base64 -d > /tmp/gigachat-cert.pem
printf '%s' "$GIGACHAT_KEY_B64" | base64 -d > /tmp/gigachat-key.pem

openssl x509 -in /tmp/gigachat-cert.pem -noout -subject -issuer
openssl rsa -in /tmp/gigachat-key.pem -check -noout

openssl x509 -noout -modulus -in /tmp/gigachat-cert.pem | openssl md5
openssl rsa -noout -modulus -in /tmp/gigachat-key.pem | openssl md5
```

Два последних хеша должны совпасть. Если `openssl rsa` просит пароль, ключ
зашифрован; приложению нужен ключ без passphrase:

```bash
openssl rsa -in client.key -out client.unencrypted.key
```

Конвертация PKCS#12 (`.pfx` / `.p12`) в PEM:

```bash
openssl pkcs12 -in client.pfx -clcerts -nokeys -out client.pem
openssl pkcs12 -in client.pfx -nocerts -nodes -out client.key
```

Конвертация DER (`.cer` / `.crt`) в PEM:

```bash
openssl x509 -inform der -in client.cer -out client.pem
```

Перекодирование PEM в base64:

```bash
base64 -i client.pem | tr -d '\n'   # macOS
base64 -i client.key | tr -d '\n'   # macOS
base64 -w0 client.pem               # Linux
base64 -w0 client.key               # Linux
```

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
uv run pytest -q
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
