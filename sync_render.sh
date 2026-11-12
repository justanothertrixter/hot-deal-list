#!/usr/bin/env bash
# HOT DEAL LIST — синхронизация каталога на Render.
#
# list.am блокирует IP дата-центров, поэтому скрапер работает только с Mac
# (резидентный IP). Этот скрипт:
#   1. собирает данные (глубокий скан);
#   2. коммитит обновлённую hot_deals.db;
#   3. пушит в GitHub -> Render автоматически передеплоивается с свежими данными.
#
# Запуск вручную:      ./sync_render.sh
# По расписанию (cron): 0 */6 * * * cd /path/to/project && ./sync_render.sh >> sync.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/3] Сбор данных (глубокий скан)…"
.venv/bin/python app.py --collect --deep

echo "[2/3] Коммит обновлённой БД…"
git add -f hot_deals.db
if git diff --cached --quiet; then
  echo "Изменений нет — выходим."
  exit 0
fi
git -c user.email="team@hotdeallist.local" -c user.name="HOT DEAL LIST" \
    commit -m "data: update $(date '+%Y-%m-%d %H:%M')"

echo "[3/3] Пуш в GitHub (Render передеплоит)…"
git push origin main

echo "Готово: каталог синхронизирован, Render обновляется автоматически."
