#!/bin/sh
# Claude SessionEnd -> нудж куратору собрать свежие durable-факты.
# Ставится как SessionEnd-хук в ~/.claude/settings.json, ловит выход любой
# головы-Claude. Fire-and-forget: не блокирует завершение сессии. Часовой sweep
# автоматизации куратора остаётся страховкой для сессий без SessionEnd (убитый
# pty, вечно живой интерактивный секретарь).
#
# Инварианты:
#  - НЕ триггерим на выходе самого куратора (cwd == ~/curator) -> иначе петля.
#  - Дебаунс: схлопываем всплеск SessionEnd (напр. при остановке Orca) в один
#    прогон. Пропущенное не теряется — watermark инкрементален, добьёт след.
#    триггер или часовой sweep.

CURATOR_ID="c38765f3-f572-4326-b9f4-3366e025cf28"
CURATOR_DIR="/home/dev/curator"
DEBOUNCE_SECS=120
STAMP="$CURATOR_DIR/state/.last-session-trigger"

payload=$(cat)

# cwd завершившейся сессии Claude кладёт в JSON хука.
cwd=$(printf '%s' "$payload" | python3 -c 'import sys, json
try:
    print(json.load(sys.stdin).get("cwd", ""))
except Exception:
    print("")' 2>/dev/null)

# Свои сессии не курируем — выход куратора не должен звать куратора.
case "$cwd" in
  "$CURATOR_DIR" | "$CURATOR_DIR"/*) exit 0 ;;
esac

# Дебаунс по mtime-независимому таймстампу.
now=$(date +%s)
if [ -f "$STAMP" ]; then
  last=$(cat "$STAMP" 2>/dev/null || echo 0)
  [ "$((now - last))" -lt "$DEBOUNCE_SECS" ] && exit 0
fi
echo "$now" > "$STAMP" 2>/dev/null

# Fire-and-forget. setsid отвязывает прогон в новую сессию — SessionEnd рвёт
# процесс Claude сразу, а без detach фоновый orca снесло бы при teardown.
setsid orca automations run "$CURATOR_ID" >/dev/null 2>&1 < /dev/null &
exit 0
