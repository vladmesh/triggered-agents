"""Initial prompt delivery confirmation for TUI heads."""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable

from . import terminal_session

TUI_DELIVERY_RETRIES = int(os.environ.get("TA_TUI_DELIVERY_RETRIES", "2"))
TUI_DELIVERY_CHECK_DELAY_S = float(os.environ.get("TA_TUI_DELIVERY_CHECK_DELAY_S", "0.5"))


class InjectDeliveryError(RuntimeError):
    """The TUI prompt stayed in the composer after delivery retries."""


def _prompt_signature(prompt: str) -> str:
    for token in ("TASK.md", "REVIEW.md"):
        if token in prompt:
            return token
    words = re.findall(r"\S+", prompt)
    return " ".join(words[:6])


def _prompt_still_in_codex_composer(screen: str, prompt: str) -> bool:
    """Return true when the initial prompt is still visible in the Codex composer."""
    screen = terminal_session.strip_ansi(screen)
    marker = screen.rfind("\u203a")
    if marker < 0:
        return False
    composer = screen[marker:]
    signature = _prompt_signature(prompt)
    return bool(signature and signature in composer)


def confirm_initial_prompt_delivered(
    prompt: str,
    read_screen: Callable[[], str],
    send_enter: Callable[[], None],
    check_delay_s: float = TUI_DELIVERY_CHECK_DELAY_S,
) -> None:
    for attempt in range(TUI_DELIVERY_RETRIES + 1):
        if check_delay_s > 0:
            time.sleep(check_delay_s)
        screen = read_screen()
        if not _prompt_still_in_codex_composer(screen, prompt):
            return
        if attempt < TUI_DELIVERY_RETRIES:
            send_enter()
            continue
    raise InjectDeliveryError(
        f"inject не доставлен: prompt остался в composer после "
        f"{TUI_DELIVERY_RETRIES + 1} проверок")
