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
  Скилл — `.claude/skills/curate`. `ephemeral = true` в `automation.toml` (triggered-agents-445):
  каждый тик — новый provider-процесс без транскрипта прошлого тика, см. «Lifecycle куратора» ниже.
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

## Lifecycle куратора

Владелец жизненного цикла терминала — `triggered_agents/runtime/dispatch.py` (singleton-драйвер,
общий для curator/retro/steward, вызывается из `ta-<agent>.timer` через `ta-gate.sh`). Для агента
с `ephemeral = true` в `automation.toml` (сейчас только curator) он не переиспользует тёплый
терминал через `/clear`: каждый тик, заставший терминал idle (прошлый прогон завершился, успешно
или с ошибкой) или busy дольше `WATCHDOG_SECONDS` (завис), останавливает весь воркспейс (`terminal
stop --worktree`), **проверяет** через повторный `terminal list`, что воркспейс действительно
опустел (`_stop_and_confirm` — код `terminal stop` сам по себе не считается надёжным сигналом),
сразу же реапит осиротевший tab (`session.tabs.close` через `_reap_ghosts`) и только после этого
поднимает свежий процесс `claude` — без `--resume`/`--continue`, то есть без транскрипта и
контекста прошлого тика. Если stop не подтвердился, тик логирует `*-stop-failed` и НЕ создаёт
новый терминал (чтобы не получить два живых процесса на одного singleton-агента) — досводит
следующий тик. retro/steward таким флагом не помечены и продолжают тёплый reuse — это отдельное
решение, не часть triggered-agents-445.

`ta-gate.sh` дёргает `dispatch` только когда precheck сигналит `rc=0` (есть новые сессии для
куратора); на `rc=100` (skip — новых сессий нет) он для ЛЮБОГО агента вместо простого `exit 0`
вызывает `dispatch --cleanup-only`. Без этого second call завершённый ephemeral-прогон мог бы
провисеть сколь угодно долго: `dispatch` (а значит и все kill-пути внутри него) иначе не вызывался
бы вовсе, пока не найдётся тик с реальной новой работой. `--cleanup-only` никогда не создаёт
терминал и не трогает ещё работающий (busy-fresh) — только доводит уже finished/stuck терминал
ephemeral-агента до нуля (`cleanup-teardown`/`cleanup-watchdog-stop` в `runs.jsonl`) и оставляет
воркспейс пустым до следующего реального диспатча.

Этот же флаг для НЕ-ephemeral агента (retro/steward) и для `pipeline` (детерминированный
диспетчер, не эта singleton-driver машинерия вовсе) — гарантированный no-op: `dispatch.run`
проверяет `_is_ephemeral(agent)` в самом начале, ДО единого Orca/board-вызова (ни `terminal list`,
ни ghost-реап, ни lookup steward-репорта), а `__main__.main`'s pipeline-ветка явно возвращает `0`
на `--cleanup-only`, не доходя до `dispatcher.tick()`. И то, и другое важно: без первого проверка
у retro/steward превратила бы их тихий skip в реальные Orca-вызовы, каких раньше не было; без
второго pipeline на каждом quiet-тике (раз в 3 минуты) гонял бы полный reconcile/advance/validate/
claim вместо честного «нечего делать».

Cleanup для curator также сводит к нулю накопившийся «сирота»-терминал, которого `_agent_terminals`
не узнаёт по title/handle (например, застрявший на дефолтном шелл-заголовке хвост старого
инцидента): и обычная ветка «нет терминала» перед созданием, и `--cleanup-only` при пустом
распознанном списке дополнительно проверяют `_raw_terminal_count` — сырой, нефильтрованный
`terminal list` для воркспейса — и, если там что-то есть, останавливают и реапят ВСЮ рабочую
папку (`_stop_and_confirm_workspace_empty`) прежде чем создавать новый терминал или объявлять
воркспейс чистым.

Диагностика зависшего прогона куратора:

1. `state/curator/runs.jsonl` (или `TA_STATE/curator/runs.jsonl` на хосте) — последняя запись:
   `action` показывает, что сделал последний тик (`created`/`ephemeral-restart`/`busy-skip`/
   `watchdog-restart`/`cleanup-teardown`/`cleanup-watchdog-stop`/`recent-create-guard`/
   `stray-sweep-failed`/`cleanup-stray-swept`/`cleanup-stray-sweep-failed`/`*-stop-failed`/...),
   `ts` — когда.
2. `orca terminal list --worktree path:<curator-воркспейс> --json` — живой терминал должен быть
   максимум один, с заголовком `triggered-agent:curator`. Больше одного или заголовок, оставшийся
   на дефолте шелла (`dev@host: ...`), — сам по себе аномалия (см. `_ensure_claude_ready` в
   `dispatch.py` про терминал, зависший на онбординге).
3. Если терминал есть, но не отвечает дольше `WATCHDOG_SECONDS` (по умолчанию 1200s,
   `TA_WATCHDOG_SECONDS`) — следующий тик (реальный диспатч или `--cleanup-only` на precheck-skip)
   сам остановит его; вручную дожимать не нужно, watchdog идемпотентен. Если `runs.jsonl` подряд
   показывает `*-stop-failed`, это значит `terminal stop` не может реально убить pty — это уже
   аномалия уровня Orca/хоста, не куратора, разбираться туда.
4. `recent-create-guard` в `runs.jsonl` без последующего `created`/`ephemeral-restart` в течение
   `CREATE_VISIBILITY_GRACE_S` (60s) — терминал, скорее всего, создался, но не стал виден Orca;
   смотреть `orca terminal list` напрямую.
5. Если после нескольких тиков в воркспейсе всё ещё видны лишние терминалы или `pending-handle`
   вкладки в `session.tabs.listAll` — это не жизнеспособное штатное состояние: `dispatch.run`
   (в т.ч. `--cleanup-only` на каждом precheck-skip тике) гарантированно сводит их к нулю, так что
   расхождение указывает на баг в самом драйвере или на ручное вмешательство в обход
   `ta-curator.timer` (см. предупреждение про единственного владельца расписания выше).

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
