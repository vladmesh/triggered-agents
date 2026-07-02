"""Layer-3 reviewer prompt — the one-time REVIEW.md handed to the independent review head.

Validate layer 3 (design-task-pipeline.md, «LLM-ревью против спеки»): once the cheap mechanical
layers are green (CI, and the stand for stand projects), the dispatcher spawns a fresh Claude head
that is NOT the card's worker and has no write access to the code. It reads the whole repo and the
full PR (not just the diff) and posts one structured verdict, then the dispatcher acts on it like
it acts on a worker report.

This module only builds the text of REVIEW.md. The host side (worktree + head) lives in worker.py,
so the dispatcher keeps talking to a single host boundary. The thermo-nuclear quality lens is not
copied into the source: the skill file is read at build time and embedded verbatim into the prompt
(design decision: «использовать как есть, вчитывать файл в промпт ревьюера, не копировать в код»),
falling back to a load-it-yourself instruction if the file is missing.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import naming

THERMO_SKILL = Path(os.environ.get(
    "TA_THERMO_SKILL",
    str(Path.home() / ".claude/skills/thermo-nuclear-code-quality-review/SKILL.md")))


def _quality_lens() -> str:
    """The thermo-nuclear skill, read from disk and embedded. Never hardcode its content — read
    the current file so the lens tracks the skill, and degrade to a pointer if it is absent."""
    try:
        return (f"Ниже — модуль качества (скилл thermo-nuclear) целиком; применяй его как есть:\n\n"
                f"````\n{THERMO_SKILL.read_text(encoding='utf-8').strip()}\n````")
    except OSError:
        return (f"Модуль качества — скилл thermo-nuclear по пути `{THERMO_SKILL}` "
                f"(не прочитался при сборке промпта). Загрузи его сам и применяй как есть.")


def build_task(card: dict, ref: str, pr: str, spec: str, base_branch: str) -> str:
    """REVIEW.md for the reviewer head: what to review, the three lenses, the blocking semantics,
    and how to emit the verdict + Идеи cards through board-CLI. `spec` is the card description."""
    project = card.get("project", "?")
    review_branch = naming.reviewer_branch(ref)
    lines = [
        f"# Ревью задачи {ref} ({project}) — слой 3 валидации",
        "",
        "Ты — независимая голова-ревьюер. Ты НЕ воркер этой карточки и прав на код нет: не коммить, "
        "не пушь, не меняй PR. Твои единственные артефакты — один вердикт-коммент и, при "
        "необходимости, карточки-идеи. Нижние слои валидации (CI, для стенд-проектов ещё стенд+e2e) "
        "уже зелёные — твоя работа поверх них.",
        "",
        "## Что вычитать",
        "",
        f"PR карточки: {pr}",
        f"База проекта: `{base_branch}`.",
        f"Воркспейс уже стоит на состоянии PR — своя ветка `{review_branch}` заведена от головы PR "
        "при подъёме, чекаутить/переключать ветку не нужно. Тебе доступен весь репо и полный PR, "
        "не только дифф:",
        "```",
        f"gh pr diff {pr}       # дифф PR, если нужен именно дифф, а не полный код",
        "```",
        f"Читай нужные файлы репо на состоянии PR, а не только строки диффа. Своя ветка "
        f"`{review_branch}` — только твоя рабочая копия, её (как и любую ветку) не пушить.",
        "",
        "## Спека карточки (по ней проверяешь criterion за criterion)",
        "",
        spec or "(описание карточки пустое)",
        "",
        "## Три линзы (все обязательны)",
        "",
        "### 1. Спека-комплаенс",
        "По каждому acceptance criterion спеки реши: выполнен РЕАЛЬНО или НА БУМАГЕ (заявлен в "
        "отчёте, но код/тест этого не делает, проверка мокнута насквозь, criterion обойдён). "
        "Для каждого — вердикт реально/на бумаге с обоснованием из кода.",
        "",
        "### 2. Adversarial-охота за багами",
        "Ищи дефекты по классам отказов; ОБЯЗАТЕЛЬНЫ все:",
        "- **Пути ошибок**: что происходит при падении каждой стадии/вызова (исключение проглочено? "
        "частичное состояние? ретрай вечно?).",
        "- **Гонки/конкурентность**: параллельные тики/воркеры/хендлы, общий стейт, лок-файлы, "
        "read-check-write без атомарности.",
        "- **Залипание навсегда без сигнала человеку**: может ли карточка/процесс встать так, что "
        "никто не узнает (нет вотчдога, нет эскалации, бесконечный ретрай, потерянный хендл).",
        "- **Утечка секретов** в комменты/логи/доску (токены, env, ключи — что постится без scrub).",
        "- **Blast radius на соседние системы**: задевает ли изменение чужие контейнеры/ветки/"
        "стейт/доску за рамками своей задачи.",
        "Каждая находка — с конкретным файлом и сценарием поломки (какой вход/состояние → что "
        "ломается).",
        "",
        "### 3. Качество кода (thermo-nuclear)",
        _quality_lens(),
        "",
        "## Семантика блокировки (важно — что блокер, а что нет)",
        "",
        "- **Блокеры** (красный вердикт): дифф самого PR подрывает качество кода и есть конкретное "
        "исправимое замечание; ЛИБО файл переваливает за 1000 строк из-за этого PR; ЛИБО баг любого "
        "из классов линзы 2; ЛИБО criterion выполнен только на бумаге.",
        "- **НЕ блокеры**: pre-existing долг, находки в СОСЕДНЕМ коде (не тронутом PR), амбициозные "
        "перестройки за рамками карточки. Их НЕ пихай в красный вердикт — оформляй карточками в "
        "колонку Идеи (см. ниже). Это единственное исключение из «прав на код нет».",
        "",
        "## Как отдать результат",
        "",
        "Один вердикт-коммент через board-CLI. Красный — если есть блокер любой линзы; иначе "
        "зелёный. Тело вердикта:",
        "1. По каждому criterion спеки: реально / на бумаге + почему.",
        "2. Находки, ранжированные блокер / замечание, КАЖДАЯ с файлом и сценарием поломки.",
        "",
        "```",
        "# красный (есть блокеры), тело обязательно:",
        f"python3 -m triggered_agents pipeline --role reviewer verdict --ref {ref} --kind red --body-file <файл>",
        "# зелёный (блокеров нет):",
        f"python3 -m triggered_agents pipeline --role reviewer verdict --ref {ref} --kind green --body-file <файл>",
        "```",
        "",
        "Не-блокеры (соседний код, долг, идеи за рамками) — карточками в Идеи:",
        "```",
        f"python3 -m triggered_agents pipeline --role reviewer idea --project {project} "
        "--title '<кратко>' --description-file <файл>",
        "```",
        "",
        "Постишь РОВНО ОДИН вердикт. Не двигай карточку и не пиши в код — на красный её вернёт "
        "диспетчер сам.",
    ]
    return "\n".join(lines)
