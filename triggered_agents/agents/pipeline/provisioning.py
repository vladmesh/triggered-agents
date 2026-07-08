"""Provision-agent policy for missing or broken workspace manifests.

The dispatcher stays deterministic: this module decides when a Ready claim should start the
manifest author/repair head, renders that head's TASK.md and recognizes the repair marker in the
card journal. The LLM is still outside the hot path for projects with a visible manifest.
"""
from __future__ import annotations

import os
from . import model, naming, worker

PROVISION_HEAD = os.environ.get("TA_PROVISION_HEAD", "codex-extra")
MODE = "provision"


def requested(comments: list[dict]) -> bool:
    """Whether the latest provision request is still open."""
    last_request = -1
    last_done = -1
    request_marker = f"[{model.MARKER_PROVISION_REQUEST}]"
    done_marker = f"[{model.MARKER_PROVISION_DONE}]"
    for i, comment in enumerate(comments):
        text = comment.get("text") or ""
        if request_marker in text:
            last_request = i
        if done_marker in text:
            last_done = i
    return last_request > last_done


def is_environment_escalation(text: str) -> bool:
    """Explicit worker report marker for "setup/provision is broken, call the repair head"."""
    lowered = text.lower()
    markers = (
        "environment: broken",
        "env: broken",
        "environment-broken",
        "окружение сломано",
        "среда сломана",
    )
    return any(marker in lowered for marker in markers)


def workspace_id(card: dict) -> str:
    base = naming.provision_workspace_base(naming.card_id(card["reference"]), naming.card_slug(card))
    project = card.get("project") or ""
    return naming.dedupe(base, lambda n: worker.workspace_exists(project, n))


def visible_manifest(project: str, base_branch: str | None = None) -> dict:
    return worker.manifest_status(project, base_branch)


def task_md(card: dict, view: dict, base: str, reason: str, manifest: dict, workspace: str) -> str:
    ref = card["reference"]
    project = card.get("project") or "?"
    project_root = worker.project_root(project)
    central = worker.CONTROL_PANEL / "pipeline" / "manifests" / f"{project}.toml"
    branch = naming.provision_branch(ref)
    current = manifest.get("path") or "(не найден)"
    comments = view.get("comments") or []
    lines = [
        f"# Провижининг окружения {ref} ({project})",
        "",
        f"Роль на доске - worker. Воркспейс уже стоит на ветке `{branch}`. Не реализуй исходную "
        "продуктовую задачу из карточки. Эта сессия должна только собрать или починить манифест "
        "окружения для task-pipeline.",
        "",
        "Done для тебя: манифест виден диспетчеру, setup+smoke проходят через "
        "`python3 /home/dev/control-panel/pipeline/provision.py --worktree <путь>`, изменения "
        "закоммичены и запушены, PR открыт или уже смержен там, где менялся манифест. В коммитах "
        "и PR никаких упоминаний AI и Co-Authored-By.",
        "",
        f"Причина запуска: {reason}. Сейчас видимый манифест: {current}.",
        "",
        "Куда писать манифест:",
        "",
        f"- Обычный проект: `{workspace}/workspace.toml` в этом worktree. После merge он станет "
        f"`{project_root / 'workspace.toml'}`.",
        f"- Контриб-форк или проект, куда нельзя коммитить напрямую: `{central}` в control-panel.",
        "",
        "Формат не меняй. Используй существующие образцы `workspace.toml` и "
        "`/home/dev/control-panel/pipeline/manifests/*.toml`. Для этой задачи нужны только базовые "
        "секции `[workspace]`, `[secrets]`, `[github]`, `[skills]`, `[setup]`, `[smoke]`. "
        "`[stand]`, `[e2e]` и `[deploy]` не добавляй без явной необходимости из карточки.",
        "",
        "Проверка перед отчётом:",
        "",
        f"- Запусти `python3 /home/dev/control-panel/pipeline/provision.py --worktree {workspace}`.",
        "- Если открыл PR и не можешь довести манифест до состояния, где следующий свежий "
        "worktree увидит его на base branch или в central manifest, отчитайся `blocked`, укажи PR "
        "и что ждёт merge.",
        "- `done` пиши только когда следующий claim этой же карточки сможет пройти обычный "
        "setup+smoke без повторного provision-agent.",
        "",
        "Отчёт через board-CLI:",
        "",
        f"`pipeline --role worker report --ref {ref} --kind "
        "done|blocked --body-file <файл>`",
        "",
        "В `done` отчёте обязательно укажи строки:",
        "",
        "```",
        "manifest: <путь к workspace.toml или central manifest>",
        "pr: <URL PR или 'merged/direct commit <sha>'>",
        "smoke: <команда и результат>",
        "```",
        "",
        "Память:",
        "",
        "Перед разбором проекта поищи в общей памяти MCP `memory_search`, сначала "
        f'`scope="project:{project}"`, затем без scope. `caller="worker"`.',
        "",
        "## Исходная спека карточки",
        "",
        view.get("description") or "(описание карточки пустое)",
        "",
    ]
    if comments:
        lines += ["## Журнал карточки", ""]
        for c in comments:
            text = (c.get("text") or "").strip()
            if text:
                lines += [text, ""]
    return "\n".join(lines).rstrip() + "\n"
