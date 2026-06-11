#!/usr/bin/env bash
#
# Локальный запуск приложения без Docker.
#
# Приложение работает локальным процессом, но подключается к НАСТОЯЩИМ
# PostgreSQL и GigaChat — адреса и креды берутся из .env.
#
# Скрипт одной командой:
#   1. синхронизирует локальное окружение через uv;
#   2. применяет миграции Alembic к указанной в .env базе;
#   3. запускает uvicorn с авто-перезагрузкой.
#
# Использование:  ./scripts/run_local.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

# 1. Локальное окружение и зависимости.
if ! command -v uv >/dev/null 2>&1; then
    echo "!! uv не найден. Установите uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi
echo ">> Синхронизирую локальное окружение через uv..."
uv sync --frozen --all-extras

# 2. .env с РЕАЛЬНЫМИ настройками. Без него дальше идти нельзя — иначе
#    приложение пойдёт на плейсхолдерную БД из шаблона.
if [ ! -f .env ]; then
    cp .env.example .env
    echo
    echo ">> Создан .env из шаблона. Заполните его РЕАЛЬНЫМИ значениями:"
    echo "   - DATABASE_DSN  — libpq DSN с адресом и кредами PostgreSQL"
    echo "     (пример: host=... port=5432 dbname=... user=... password='p@ss w\$rd')"
    echo "   - DB_SCHEMA     — схема (по умолчанию templater)"
    echo "   - GIGACHAT_*    — URL и сертификаты GigaChat (base64 PEM)"
    echo "   Затем запустите ./scripts/run_local.sh снова."
    echo
    exit 1
fi

# 3. Миграции Alembic в настоящую БД.
echo ">> Применяю миграции Alembic..."
echo "   (ошибка подключения здесь = PostgreSQL недоступен или неверный DATABASE_DSN)"
uv run alembic upgrade head

# 4. Запуск приложения.
echo ">> Запускаю приложение на http://127.0.0.1:8000  (Ctrl+C для остановки)"
exec uv run uvicorn app.main:app --reload
