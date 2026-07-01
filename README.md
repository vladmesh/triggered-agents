# triggered-agents

Рантайм крон/событийных агентов секретаря (часть субстрата control-panel). Каждый агент —
headless-прогон по триггеру (таймер или событие): собрать разнородные живые источники, свести
LLM-суждением в канонический артефакт, закоммитить. Проекция живого состояния в канон.

## Устройство

- `triggered_agents/runtime/` — общий рантайм: `state.py` (watermark + lock по агенту),
  `redact.py` (вырезание секретов). CLI-диспетч — `triggered_agents/__main__.py`.
- `triggered_agents/agents/<name>/` — плагин агента: `discover`/`harvest` (свои источники),
  `cli.py` (команды, которые скилл дёргает через Bash). Суждение живёт в Orca-скилле агента.
- `state/<agent>/` — watermark и лок агента (gitignored, у каждого агента свой).
- `hooks/` — event-триггеры (напр. `session-end-trigger.sh` куратора).

CLI: `python3 -m triggered_agents <agent> <cmd>` (`harvest`/`advance`/`precheck`/`sessions`/`status`).

## Агенты

- **curator** (первый плагин) — единственный writer канона памяти (`panelmem-kb`). Агенты память
  только читают. Смотрит следы всех голов (парсер на голову), вытаскивает durable-факты,
  дедупит/суперсидит, коммитит И пушит канон (обычный push, без `--force`) как офф-бокс-бэкап.
  Индекс не трогает — его ребилдит memory-mcp. Себя не харвестит (`~/triggered-agents` исключён).
  Скилл — `.claude/skills/curate`.

## Как ложится на Орку

Проект `triggered-agents` = этот репо. Каждый агент = свой Orca-воркспейс (worktree) + автоматизация,
привязанная к нему (`reuseSession`, свой `provider`/`prompt`/`precheck`). Скилл выбирается промптом.
Реальные часы — systemd-таймер (`orca automations run <id>`); встроенный rrule в headless serve не
тикает. Дизайн и инварианты — `control-panel/docs/ARCHITECTURE.md` и `docs/backlog.md`.

## Инварианты

- Один сериализованный прогон на агента (лок/watermark), не спавн по сессии.
- Watermark/курсор по источнику: обрабатываем только новое; двигаем ТОЛЬКО после успешного коммита.
- Этот репо (код) сам не коммитит/не пушит без явной просьбы. Каноны-выходы — отдельные репо.
