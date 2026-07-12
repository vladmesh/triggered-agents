# Changelog

Заметные изменения рантайма triggered-agents и общих машин пайплайна. Новое сверху. Устройство —
`README.md` и `AGENTS.md`; каждая строка — что изменилось в поведении, без перебора альтернатив.

## Куратор: fresh-session lifecycle вместо тёплого reuse

- `runtime/dispatch.py` больше не `/clear`-ит и не переиспользует тёплый терминал куратора: спека
  `curator/automation.toml` теперь ставит `ephemeral = true`, и singleton-драйвер, застав такой
  агент idle (прошлый тик завершился) или stuck дольше `WATCHDOG_SECONDS`, останавливает весь
  воркспейс и поднимает новый `claude`-процесс без `--resume`/`--continue` — каждый тик стартует с
  чистого provider session, без транскрипта и контекста предыдущего прогона.
- Оба kill-пути (ephemeral-teardown и watchdog-restart) реапят собственный ghost tab сразу после
  `terminal stop`, а не полагаются на реап в начале следующего тика — после успешного завершения,
  ошибки или watchdog-таймаута ни PTY, ни tab завершённого прогона не остаются висеть.
- `curator/automation.toml` дополнительно ставит `reuse_session = false`, чтобы и ручной
  диагностический `orca automations run` не попадал в тёплую Orca-сессию с чужим транскриптом.
- retro/steward не тронуты — `ephemeral` читается best-effort и по умолчанию `false`, их
  `/clear`-based reuse не изменился; переход на ephemeral lifecycle для них не в скоупе этой
  карточки.
- `deploy/ta-gate.sh` на precheck `rc=100` (нет новых сессий для куратора) больше не просто
  выходит: вызывает `dispatch --cleanup-only`. Без этого `dispatch` (и все kill-пути внутри него)
  вообще не вызывался бы на тике без новой работы — уже завершённый ephemeral-прогон куратора мог
  провисеть неограниченно, пока не найдётся тик с реальной работой. `--cleanup-only` никогда не
  диспатчит скилл и не создаёт терминал; для ephemeral-агента он доводит finished/stuck терминал до
  нуля тем же `_stop_and_confirm`+`_reap_ghosts`, для остальных — no-op.
- Каждый путь «stop, затем create» (watchdog-restart, ephemeral-restart, red-fallback) теперь
  проверяет через повторный `terminal list`, что воркспейс действительно опустел
  (`_stop_and_confirm`), прежде чем поднимать замену — код возврата `terminal stop` сам по себе
  не считается надёжным сигналом. Неподтверждённый stop логируется как `*-stop-failed` и НЕ ведёт
  к созданию второго живого терминала для одного singleton-агента; досводит следующий тик.
- Ветка «нет терминала» перед созданием проверяет `AgentState.load_terminal_created_at()` против
  `CREATE_VISIBILITY_GRACE_S` (60s): терминал, который этот же агент только что создал, может ещё
  не быть виден в `terminal list`, и второй тик, попавший в этот разрыв, больше не читает это как
  «ничего не спавнилось» и не создаёт дубликат — вместо этого логирует `recent-create-guard`.
- `README.md` — раздел «Lifecycle куратора» расширен: `--cleanup-only`-путь, `_stop_and_confirm`,
  новые `action`-значения в `runs.jsonl`, обновлённая диагностика зависшего прогона.

Второй раунд ревью (PR #95) нашёл, что `--cleanup-only`, отправляемый теперь на КАЖДОМ
precheck-skip любого агента, задел два соседних, вне-скоуп пути, и что cleanup куратора не видел
часть живых orphan-терминалов:

- `triggered_agents/__main__.py`: pipeline-ветка (детерминированный диспетчер, без terminal/PTY
  вовсе) раньше игнорировала `--cleanup-only` и на каждом quiet-тике (раз в 3 минуты) всё равно
  гоняла полный `dispatcher.tick()` — теперь на `--cleanup-only` она сразу возвращает `0`.
- `runtime/dispatch.py`: `run()` для НЕ-ephemeral агента (retro/steward) с `cleanup_only=True`
  теперь выходит ДО первого Orca/board-вызова — раньше ghost-реап и `terminal list` успевали
  выполниться до собственной no-op проверки `_cleanup_only`, то есть их precheck-skip перестал
  быть тем же нулевым по Orca-вызовам no-op, каким был до этой карточки.
- `_agent_terminals`'s фильтр по title/handle может не узнать реально живой сирота-терминал
  (застрявший на дефолтном шелл-заголовке хвост старого инцидента) — и ветка «нет терминала», и
  `--cleanup-only` при пустом распознанном списке теперь дополнительно проверяют
  `_raw_terminal_count` (сырой `terminal list`, без фильтра) и подметают воркспейс целиком
  (`_stop_and_confirm_workspace_empty`) прежде чем создавать новый терминал или объявлять cleanup
  завершённым — иначе такой сирота пережил бы вообще любое число тиков.
- `tests/test_runtime_dispatch.py` разнесён: общая fixture `_DispatchBase` переехала в
  `tests/_dispatch_fixtures.py`, ephemeral/cleanup-only/stray-sweep/two-tick тесты — в новый
  `tests/test_dispatch_lifecycle.py` (файл на главной ветке уже был 640 строк, а с первым раундом
  правок вырос за 1000).

Третий раунд ревью (PR #95) указал на структурную проблему, которую первые два раунда не решали:
cleanup для ephemeral-агента был по-прежнему привязан к тому, что его КТО-ТО ЗАМЕТИТ на будущем
тике (реальный dispatch или `--cleanup-only`), а не к самому факту завершения головы.

- `runtime/dispatch.py`: `_create_terminal` для ephemeral-агента дописывает к команде запуска
  головы self-teardown trailer через обычный `;` (не `&&`, чтобы отработал независимо от кода
  выхода головы) — `_with_finalizer`. Как только процесс головы завершается (успешно, с ошибкой,
  любым выходом, после которого shell доходит до следующей команды), ЭТОТ ЖЕ терминал сам себя
  останавливает: новая команда `dispatch <agent> --finalize` → `dispatch.finalize()` →
  `_stop_and_confirm_workspace_empty` + `_reap_ghosts`. Cleanup впервые оказался привязан к
  реальному событию завершения, а не к опросу на следующем тике.
- `finalize()` берёт тот же `state.lock()`, что и `run()` — без этого self-referential `terminal
  stop --worktree` (убивает вообще все терминалы в воркспейсе, не только «свой») мог бы
  состязаться с параллельным тиком и убить только что созданную им замену. При занятом локе
  finalize просто откатывается (`state.lock()` кидает исключение), ничего не трогая — уборку в
  этом случае доводит тот тик, что держит лок.
- Все прежние kill-пути (idle-ephemeral, watchdog, `--cleanup-only`, stray-sweep) остаются —
  теперь это явный бэкап на случай, когда терминал вообще не доходит до собственного trailer
  (hard kill, перезагрузка хоста, рестарт Orca), а не главный механизм.
- `triggered_agents/__main__.py` разбирает новый флаг `--finalize` и роутит его в
  `dispatch.finalize` (не в `dispatch.run`); для `pipeline` (без terminal/PTY) — тот же
  безусловный no-op, что и `--cleanup-only`.
- Новые тесты: `tests/test_dispatch_lifecycle.py::FinalizerTest` (trailer действительно дописан
  для ephemeral и не дописан для не-ephemeral; `finalize()` тушит после подтверждённого stop,
  логирует failure без исключения при неподтверждённом, no-op для не-ephemeral, откатывается при
  занятом локе) и новый тест в `EphemeralTwoTickLifecycleTest`, доказывающий обнуление
  терминалов/вкладок ОДНИМ вызовом `dispatch.finalize()` без единого повторного тика.
- `README.md`/`CHANGELOG.md` — раздел «Lifecycle куратора» переписан: self-teardown trailer как
  главный механизм, прежние poll-based пути — как бэкап.

Четвёртый раунд ревью (PR #95) закрыл три оставшихся блокера в фактическом соблюдении гарантий
teardown:

- `_raw_terminal_count` теперь отличает пустой список от нечитаемого: при сбое/таймауте `terminal
  list` он возвращает `None` (неизвестно), а не `0`. `_stop_and_confirm_workspace_empty`
  подтверждает teardown только на реальном нуле, поэтому Orca-hiccup больше не логируется как
  здоровый `self-teardown` поверх, возможно, ещё живого pty. Ветки перед созданием и в
  `--cleanup-only` на `None` не создают/не объявляют чисто, а логируют `stray-check-failed` /
  `cleanup-stray-check-failed` и оставляют воркспейс следующему тику.
- self-teardown trailer несёт идентичность терминала — `--generation <n>` из нового монотонного
  счётчика (`AgentState.next_terminal_generation`, файл `terminal_generation.json`). `finalize`
  сравнивает своё поколение с поколением текущего терминала воркспейса: при расхождении
  (параллельный тик уже заменил терминал) он только реапит свой ghost (`self-teardown-superseded`)
  и НЕ делает blanket `terminal stop`, иначе убил бы живую замену. Счётчик не сбрасывается при
  teardown, чтобы замена не получила номер запоздавшего finalize.
- при контеншене `finalize` больше не бросает уборку сразу: повторяет попытку взять лок ограниченное
  число раз (`FINALIZE_LOCK_ATTEMPTS` × `FINALIZE_LOCK_RETRY_S`) и лишь потом логирует
  `self-teardown-deferred` и отдаёт уборку держателю лока. Ретрай безопасен благодаря
  generation-гварду выше.
- `triggered_agents/__main__.py` разбирает `--generation <n>` и передаёт его в `dispatch.finalize`.
- тесты: `tests/test_dispatch_lifecycle.py` — `RawListFailureTest` (None vs 0 во всех путях),
  новые тесты в `FinalizerTest` (supersede без остановки замены; stop при совпадении поколения;
  failure при нечитаемом raw-list; defer вместо raise при вечно занятом локе) и новый
  `EphemeralSessionIdentityTest` — интеграционный прогон с in-memory Orca, где каждый `terminal
  create` выдаёт свежий provider session id и пустой context store: доказывает разные session id
  двух прогонов, отсутствие переноса контекстного маркера из первого во второй и ноль
  терминалов/вкладок после КАЖДОГО завершения (через собственный finalize головы, с её
  generation).

Пятый раунд ревью (PR #95) нашёл, что no-op для non-ephemeral агента стоял ВНУТРИ `state.lock()`:

- `runtime/dispatch.py`: проверка `cleanup_only and not _is_ephemeral(agent)` поднята в самое
  начало `run()`, ДО конструирования `AgentState` и взятия `state.lock()`. Раньше `--cleanup-only`,
  который `ta-gate.sh` шлёт на КАЖДОМ precheck-skip любого агента, для retro/steward сначала входил
  в `with state.lock()`, а no-op стоял уже за ним — если лок держал активный детерминированный
  helper (или остался stale), тихий skip падал `SystemExit: another run holds the lock` вместо
  прежнего print-and-exit-0. `_is_ephemeral` читает только automation.toml, не лок/Orca/board.
- тест `tests/test_dispatch_lifecycle.py::CleanupOnlyTest::
  test_non_ephemeral_cleanup_only_is_a_noop_even_with_the_run_lock_held` — держит лок и проверяет,
  что non-ephemeral `--cleanup-only` возвращает 0 без `SystemExit` и без единого касания
  state/Orca.

## Куратор: единственный владелец расписания

- `deploy/provision.py` теперь явно управляет флагом `enabled` встроенной Orca-автоматизации по
  полю `[trigger].enabled` спеки агента, и на create, и на edit — стрей ручной re-enable из Orca
  GUI откатывается обратно на следующем provision, а не остаётся навсегда. По умолчанию (поле не
  задано) поведение не меняется: автоматизация включена, как раньше.
- `curator/automation.toml` ставит `[trigger].enabled = false` — `ta-curator.timer` остаётся
  единственным владельцем часового тика; `orca automations run <id>` (ручной диагностический
  запуск) не завязан на этот флаг и продолжает работать. retro/steward не тронуты, вне скоупа
  карточки.
- `steward drift` (deep-sweep) детектирует состояние `double-schedule`: живой `ta-<agent>.timer`
  на хосте одновременно с включённым триггером одноимённой Orca-автоматизации — сигнал, что один
  тик может задиспатчить скилл дважды. Проверка ограничена агентами, чья спека сама объявляет
  `[trigger].enabled = false` (curator) — retro/steward не опознаны в single-scheduler и не
  флагаются за состояние, которое эта карточка не просила менять (найдено ревью PR #94).
  Отказ запроса состояния Orca-автоматизации (сеть, неожиданный формат ответа) для опт-ин агента с
  живым таймером репортится отдельным `double-schedule-unknown`, а не молча читается как «всё
  чисто» — иначе именно тот отказ, который чек должен ловить, гасил бы сам чек (второй раунд ревью
  PR #94).

## Validate: самовосстановление при расхождении ветки с базой

- Слой 1 Validate различает свежесть ветки отдельно от CI. `poll_pr` отдаёт нормализованное
  состояние mergeability/base freshness (GitHub `mergeable`/`mergeStateStatus`, head/base SHA и
  фактическую base-ветку PR) отдельно от CI rollup; временный `UNKNOWN` не считается ни конфликтом,
  ни зелёным и падает в обычный CI-путь. Конфликтный или отставший PR больше не висит под видом
  «CI не появился».
- Ветку, отставшую от базы без текстового конфликта, pipeline обновляет сам: обычный merge
  фактической базы PR в ветку в существующем воркспейсе воркера (merge-коммит, push, без
  rebase/force-push), сброс старых CI/review/stand clocks и marker baseline, ожидание CI нового
  head SHA. Старый зелёный marker или вердикт ревьюера не переиспользуется; ревьюер не стартует до
  зелёного CI нового состояния.
- Текстовый конфликт (или незакоммиченные изменения в воркспейсе) возвращает карточку тому же
  воркеру (Validate → In progress) со списком конфликтных файлов, base/head SHA и инструкцией
  смержить/разрешить/запушить/переотчитаться. Воркспейс и feature-ветка сохраняются.
- Recovery идемпотентен по base SHA (счётчик попыток и произведённый head в записи карточки): один
  base SHA не вызывает бесконечные merge/push/relaunch; исчерпание бюджета уводит карточку в
  Blocked с сохранённым воркспейсом. Ошибки fetch/merge/push различаются в журнале, секреты
  scrubbed, частично начатый merge аборчен, а грязное дерево не трогается — незакоммиченный код
  воркера не теряется.
