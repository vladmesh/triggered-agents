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
терминал через `/clear`, и каждый тик стартует с чистого provider session — без `--resume`/
`--continue`, то есть без транскрипта и контекста прошлого тика.

Главный механизм — **самоликвидация**, а не поллинг следующим тиком: `_create_terminal` дописывает
к команде запуска головы (`_with_finalizer`) `; python3 -m triggered_agents <agent> dispatch
--finalize --generation <n>`, обычным shell `;` (не `&&`) — значит trailer выполняется всегда,
независимо от того, успешно ли завершилась голова или с ошибкой. Как только сам процесс головы
выходит, ЭТОТ ЖЕ терминал немедленно останавливает себя (`dispatch.finalize` →
`_stop_and_confirm_workspace_empty` → `_reap_ghosts`) — cleanup происходит в момент завершения, а
не когда-то потом на очередном опросе.

`finalize` берёт тот же `state.lock()`, что и `run()` (self-teardown останавливает весь воркспейс,
а `terminal stop --worktree` убивает ВСЕ его терминалы). При контеншене он не бросает уборку сразу,
а **повторяет попытку взять лок** ограниченное число раз (`FINALIZE_LOCK_ATTEMPTS` ×
`FINALIZE_LOCK_RETRY_S`) — живой тик держит лок лишь на время своих Orca-вызовов, так что короткое
пересечение не должно стоить завершённому терминалу его уборки; если тик завис на локе на всё окно,
finalize логирует `self-teardown-deferred` и отдаёт уборку ему (или следующему тику). Этот ретрай
безопасен только потому, что сам teardown защищён **идентичностью поколения**: trailer несёт
`--generation` терминала, которому принадлежит; `finalize` сравнивает его с поколением ТЕКУЩЕГО
терминала воркспейса (`AgentState.load_terminal_generation`). Если они различаются — значит
параллельный тик уже заменил терминал (его ephemeral-restart остановил старый и создал новый), и
finalize только реапит свой ghost-tab (`self-teardown-superseded`), НО не останавливает воркспейс,
иначе убил бы живую замену. Если совпадают (или поколение не записано — легаси-trailer) — под локом
никакой параллельный create невозможен, поэтому blanket stop заведомо бьёт только по своему
терминалу. Счётчик поколений (`state/curator/terminal_generation.json`) монотонный и не сбрасывается
при teardown — иначе замена могла бы получить тот же номер, что несёт запоздавший finalize.

Все остальные kill-пути (idle-ephemeral в `dispatch.run`, watchdog, `--cleanup-only`, stray-sweep)
остаются обязательным бэкапом — не главным механизмом, а подстраховкой на случай, когда терминал
вообще не добирается до своего trailer (hard kill, перезагрузка хоста, рестарт самой Orca). Для
них: заставший терминал idle (прошлый прогон завершился, но по какой-то причине finalize не
отработал) или busy дольше `WATCHDOG_SECONDS` (завис) `dispatch.run` останавливает весь воркспейс
(`terminal stop --worktree`), **проверяет** через повторный `terminal list`, что воркспейс
действительно опустел (`_stop_and_confirm` — код `terminal stop` сам по себе не считается надёжным
сигналом), сразу же реапит осиротевший tab (`session.tabs.close` через `_reap_ghosts`) и только
после этого поднимает свежий процесс. Если stop не подтвердился, тик логирует `*-stop-failed` и НЕ
создаёт новый терминал (чтобы не получить два живых процесса на одного singleton-агента) —
досводит следующий тик. retro/steward таким флагом не помечены, trailer им не дописывается, и они
продолжают тёплый reuse — это отдельное решение, не часть triggered-agents-445.

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
проверяет `_is_ephemeral(agent)` в самом начале, ДО конструирования `AgentState` и взятия
`state.lock()`, не говоря уже про Orca/board-вызовы (ни `terminal list`, ни ghost-реап, ни lookup
steward-репорта), а `__main__.main`'s pipeline-ветка явно возвращает `0` на `--cleanup-only`, не
доходя до `dispatcher.tick()`. И то, и другое важно: без первого проверка у retro/steward
превратила бы их тихий skip либо в реальные Orca-вызовы, каких раньше не было, либо — если их лок
уже держит детерминированный helper (или остался stale) — в `SystemExit: another run holds the
lock`, потому что `--cleanup-only` теперь дёргается на КАЖДОМ precheck-skip любого агента; без
второго pipeline на каждом quiet-тике (раз в 3 минуты) гонял бы полный reconcile/advance/validate/
claim вместо честного «нечего делать».

Cleanup для curator также сводит к нулю накопившийся «сирота»-терминал, которого `_agent_terminals`
не узнаёт по title/handle (например, застрявший на дефолтном шелл-заголовке хвост старого
инцидента): и обычная ветка «нет терминала» перед созданием, и `--cleanup-only` при пустом
распознанном списке дополнительно проверяют `_raw_terminal_count` — сырой, нефильтрованный
`terminal list` для воркспейса — и, если там что-то есть, останавливают и реапят ВСЮ рабочую
папку (`_stop_and_confirm_workspace_empty`) прежде чем создавать новый терминал или объявлять
воркспейс чистым. Важно: `_raw_terminal_count` отличает «ноль терминалов» от «список не удалось
прочитать» — при сбое/таймауте самого `terminal list` он возвращает `None` (неизвестно), а не `0`.
Ни confirm-путь (`_stop_and_confirm_workspace_empty` подтверждает только на реальном нуле), ни
проверка перед созданием не примут Orca-hiccup за пустой воркспейс: create-ветка логирует
`stray-check-failed`, `--cleanup-only` — `cleanup-stray-check-failed`, и оставляют воркспейс
следующему тику вместо ложного «чисто».

Диагностика зависшего прогона куратора:

1. `state/curator/runs.jsonl` (или `TA_STATE/curator/runs.jsonl` на хосте) — последняя запись:
   `action` показывает, что сделал последний тик или self-teardown (`created`/`ephemeral-restart`/
   `busy-skip`/`watchdog-restart`/`cleanup-teardown`/`cleanup-watchdog-stop`/`recent-create-guard`/
   `stray-check-failed`/`stray-sweep-failed`/`cleanup-stray-swept`/`cleanup-stray-check-failed`/
   `cleanup-stray-sweep-failed`/`self-teardown`/`self-teardown-superseded`/`self-teardown-deferred`/
   `self-teardown-failed`/`*-stop-failed`/...), `ts` — когда. `self-teardown` — самый частый случай
   в здоровом воркспейсе: голова завершилась и её же терминал сам себя убрал через `--finalize`,
   без участия следующего тика. `self-teardown-superseded` — тоже здоровый: параллельный тик успел
   заменить терминал, и finalize корректно не тронул живую замену. `self-teardown-deferred` —
   finalize не смог взять лок за отведённое окно и отдал уборку тику, который его держит; редкий,
   но не ошибка. `*-check-failed`/`*-stop-failed` — `terminal list`/`stop` не отвечает или не убил
   pty, это аномалия уровня Orca/хоста, не куратора.
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
