#!/usr/bin/env bash
#
# Локальный запуск приложения без Docker.
#
# Приложение работает локальным процессом, но подключается к НАСТОЯЩИМ
# PostgreSQL и GigaChat — адреса и креды берутся из .env.
#
# Скрипт одной командой:
#   1. создаёт виртуальное окружение и ставит зависимости (один раз);
#   2. применяет миграции Alembic к указанной в .env базе;
#   3. засеивает базовые справочники (идемпотентно);
#   4. запускает uvicorn с авто-перезагрузкой.
#
# Использование:  ./scripts/run_local.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3.11}"

# 1. Виртуальное окружение и зависимости (создаётся один раз).
if [ ! -d .venv ]; then
    echo ">> Создаю виртуальное окружение ($PYTHON) и ставлю зависимости..."
    if ! command -v "$PYTHON" >/dev/null 2>&1; then
        echo "!! $PYTHON не найден. Установите Python 3.11+ или задайте PYTHON=<путь>." >&2
        exit 1
    fi
    "$PYTHON" -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install -r requirements.txt
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 2. .env с РЕАЛЬНЫМИ настройками. Без него дальше идти нельзя — иначе
#    приложение пойдёт на плейсхолдерную БД из шаблона.
if [ ! -f .env ]; then
    cp .env.example .env
    echo
    echo ">> Создан .env из шаблона. Заполните его РЕАЛЬНЫМИ значениями:"
    echo "   - DATABASE_URL  — адрес и креды вашего PostgreSQL"
    echo "     (пароль со спецсимволами percent-encode: @->%40, \$->%24, |->%7C)"
    echo "   - DB_SCHEMA     — схема (по умолчанию templater)"
    echo "   - GIGACHAT_*    — URL и сертификаты GigaChat (base64 PEM)"
    echo "   Затем запустите ./scripts/run_local.sh снова."
    echo
    exit 1
fi

# 3. Миграции Alembic в настоящую БД.
echo ">> Применяю миграции Alembic..."
echo "   (ошибка подключения здесь = PostgreSQL недоступен или неверный DATABASE_URL)"
alembic upgrade head

# 4. Сидинг базовых справочников (идемпотентно).
echo ">> Засеиваю базовые справочники..."
python -m scripts.seed_reference_data

# 5. Запуск приложения.
echo ">> Запускаю приложение на http://127.0.0.1:8000  (Ctrl+C для остановки)"
exec uvicorn app.main:app --reload
