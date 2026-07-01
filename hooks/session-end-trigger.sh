#!/bin/sh
# Claude SessionEnd -> нудж куратору собрать свежие durable-факты.
# Ставится как SessionEnd-хук в ~/.claude/settings.json, ловит выход любой
# головы-Claude. Fire-and-forget: не блокирует завершение сессии. Часовой sweep
# автоматизации куратора остаётся страховкой для сессий без SessionEnd (убитый
# pty, вечно живой интерактивный секретарь).
#
# Инварианты:
#  - НЕ триггерим на выходе самих triggered-agents (их воркстри под
#    ~/orca/workspaces/triggered-agents) -> иначе петля.
#  - Дебаунс: схлопываем всплеск SessionEnd (напр. при остановке Orca) в один
#    прогон. Пропущенное не теряется — watermark инкрементален, добьёт след.
#    триггер или часовой sweep. Штамп — в стабильном хост-пути, не в воркстри.

CURATOR_ID="c38765f3-f572-4326-b9f4-3366e025cf28"
EXCLUDE_DIR="/home/dev/orca/workspaces/triggered-agents"
DEBOUNCE_SECS=120
STAMP_DIR="/home/dev/.local/state/triggered-agents"
STAMP="$STAMP_DIR/curator-session-trigger"

payload=$(cat)

# cwd завершившейся сессии Claude кладёт в JSON хука.
cwd=$(printf '%s' "$payload" | python3 -c 'import sys, json
try:
    print(json.load(sys.stdin).get("cwd", ""))
except Exception:
    print("")' 2>/dev/null)

# Свои сессии не курируем — выход triggered-agent не должен звать куратора.
case "$cwd" in
  "$EXCLUDE_DIR" | "$EXCLUDE_DIR"/*) exit 0 ;;
esac

# Дебаунс по mtime-независимому таймстампу.
now=$(date +%s)
if [ -f "$STAMP" ]; then
  last=$(cat "$STAMP" 2>/dev/null || echo 0)
  [ "$((now - last))" -lt "$DEBOUNCE_SECS" ] && exit 0
fi
mkdir -p "$STAMP_DIR" 2>/dev/null
echo "$now" > "$STAMP" 2>/dev/null

# Fire-and-forget. setsid отвязывает прогон в новую сессию — SessionEnd рвёт
# процесс Claude сразу, а без detach фоновый orca снесло бы при teardown.
setsid orca automations run "$CURATOR_ID" >/dev/null 2>&1 < /dev/null &
exit 0
