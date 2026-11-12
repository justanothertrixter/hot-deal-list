#!/usr/bin/env bash
# HOT DEAL LIST — синхронизация каталога на Render.
#
# list.am блокирует IP дата-центров, поэтому скрапер работает только с Mac
# (резидентный IP). Этот скрипт:
#   1. собирает данные (глубокий скан);
#   2. коммитит обновлённую hot_deals.db;
#   3. пушит в GitHub;
#   4. (опционально) дёргает Deploy Hook Render, чтобы деплой сработал гарантированно.
#
# Deploy Hook храните в файле ~/.config/hotdeal/deploy_hook (одна строка — URL)
# или в переменной окружения RENDER_DEPLOY_HOOK. В репозиторий не коммитьте.
#
# Запуск вручную:      ./sync_render.sh
# По расписанию (cron): 0 */6 * * * cd /path/to/project && ./sync_render.sh >> sync.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/4] Сбор данных (глубокий скан)…"
.venv/bin/python app.py --collect --deep

echo "[2/4] Коммит обновлённой БД…"
git add -f hot_deals.db
if git diff --cached --quiet; then
  echo "Изменений в БД нет."
else
  git -c user.email="team@hotdeallist.local" -c user.name="HOT DEAL LIST" \
      commit -m "data: update $(date '+%Y-%m-%d %H:%M')"
fi

echo "[3/4] Пуш в GitHub…"
git push origin main

echo "[4/4] Запуск деплоя на Render…"
HOOK="${RENDER_DEPLOY_HOOK:-}"
if [ -z "$HOOK" ] && [ -f "$HOME/.config/hotdeal/deploy_hook" ]; then
  HOOK="$(tr -d '[:space:]' < "$HOME/.config/hotdeal/deploy_hook")"
fi
if [ -n "$HOOK" ]; then
  if curl -fsS "$HOOK" >/dev/null; then
    echo "Render: деплой запущен."
  else
    echo "Render: не удалось дёрнуть deploy hook (проверьте URL)."
  fi
else
  echo "Render: deploy hook не настроен — задеплойте вручную (Manual Deploy)."
  echo "  Сохраните URL хука в ~/.config/hotdeal/deploy_hook, чтобы деплой шёл сам."
fi
