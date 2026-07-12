# triggered-agents

> Ссылки вида `control-panel/...` и `panelmem-kb` ведут в приватные репо этой же системы
> (мозг секретаря и канон памяти) — публичен только рантайм.

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
- **retro** (второй плагин) — ретро-пасс по свежим транскриптам голов (harvest переиспользует
  куратора) плюс memory-mcp `search-log.jsonl`: ищет фейлы (ответ по факту из panelmem без
  `memory_search` и с ошибкой, повтор известной ошибки, зацикливание, пустая сессия). Выход —
  только предложения (пункты в бэклог control-panel или PR с правкой скилла), в main ничего не
  пушит. Каденс — daily. Скилл — `.claude/skills/retro`.

## Как ложится на Орку

Проект `triggered-agents` = этот репо. Каждый агент = свой Orca-воркспейс (worktree) + автоматизация,
привязанная к нему (`reuseSession`, свой `provider`/`prompt`/`precheck`). Скилл выбирается промптом.
Реальные часы — systemd-таймер (`orca automations run <id>`); встроенный rrule в headless serve не
тикает. Дизайн и инварианты — `control-panel/docs/ARCHITECTURE.md` и `docs/backlog.md`.

## Pipeline Validate

Code и research-карточки проходят Validate послойно: PR/branch integrity, GitHub CI или явный
`[validate] ci = "none"` в манифесте проекта, stand/e2e для проектов со стендом, затем LLM-review
слоя 3 и штатный merge/Done path.

Слой 1 сперва различает свежесть ветки от CI: если голова PR разошлась с базой (GitHub
`mergeable`/`mergeStateStatus`), Validate чинит это отдельно, не давая конфликтному PR зависнуть
под видом «CI не появился». Ветку, отставшую от базы без текстового конфликта, pipeline обновляет
сам обычным merge базы в существующем воркспейсе воркера (merge-коммит, push, без rebase/force),
сбрасывает слои и ждёт CI нового head SHA. Текстовый конфликт возвращается тому же воркеру со
списком конфликтных файлов и base SHA. Попытки ограничены на один base SHA — исчерпание бюджета
уводит карточку в Blocked с сохранённым воркспейсом.

PO может отключить только слой 3 для отдельной карточки:

```bash
python3 -m triggered_agents pipeline --role po create \
  --project personal_site --type code --title "..." --column Ready \
  --slug my-task --review-head none --description-file "$spec"

python3 -m triggered_agents pipeline --role po update \
  --ref personal_site-42 --review-head none
```

Пустой `--review-head ""` на update возвращает глобальный reviewer по умолчанию. Значение `none`
пишется в metadata и видно в `list`, `show` и `TASK.md`; это audit-сигнал, а не пустой профиль.
Worker и reviewer не могут менять это поле, потому что `update` остаётся PO-only.

`none` допустим для мелких, низкорисковых или уже вручную вычитанных карточек, когда PO принимает
риск отсутствия независимой LLM-вычитки. Это не реакция на красный CI, зависший check, stand failure,
закрытый PR или недоступный reviewer: нижние механические слои обязательны и при ошибке ведут
карточку тем же путём, что и с обычным reviewer.

## Инварианты

- Один сериализованный прогон на агента (лок/watermark), не спавн по сессии.
- Watermark/курсор по источнику: обрабатываем только новое; двигаем ТОЛЬКО после успешного коммита.
- Этот репо (код) сам не коммитит/не пушит без явной просьбы. Каноны-выходы — отдельные репо.
