import unittest

from triggered_agents.agents.pipeline import prompt_delivery


class PromptDeliveryTest(unittest.TestCase):
    def test_delayed_composer_does_not_confirm_on_empty_first_screen(self):
        screens = iter([
            "",
            "│ >_ OpenAI Codex │\n› Ты — воркер task-пайплайна. TASK.md gpt-5.5 · /ws/fresh",
            "Working",
        ])
        sent = []

        result = prompt_delivery.confirm_initial_prompt_delivered(
            "Ты — воркер task-пайплайна. TASK.md",
            lambda: next(screens),
            lambda: sent.append("enter"),
            check_delay_s=0,
            poll_s=0.01,
            timeout_s=1,
            resend_grace_s=0,
        )

        self.assertEqual(result.signal, "screen-working")
        self.assertEqual(sent, ["enter"])

    def test_delayed_session_activity_confirms_without_resend(self):
        seen = {"checks": 0}

        def turn_started():
            seen["checks"] += 1
            return "session-user-turn" if seen["checks"] >= 3 else None

        result = prompt_delivery.confirm_initial_prompt_delivered(
            "read TASK.md",
            lambda: "",
            lambda: self.fail("must not resend without prompt in composer"),
            turn_started=turn_started,
            check_delay_s=0,
            poll_s=0.01,
            timeout_s=1,
        )

        self.assertEqual(result.signal, "session-user-turn")
        self.assertEqual(result.resends, 0)

    def test_resend_succeeds_when_prompt_stays_in_composer_once(self):
        screens = iter([
            "› read TASK.md gpt-5.5 · /ws/fresh",
            "Thinking",
        ])
        sent = []
        events = []

        result = prompt_delivery.confirm_initial_prompt_delivered(
            "read TASK.md",
            lambda: next(screens),
            lambda: sent.append("enter"),
            log_event=lambda **fields: events.append(fields),
            check_delay_s=0,
            poll_s=0.01,
            timeout_s=1,
            resend_grace_s=0,
        )

        self.assertEqual(result.signal, "screen-working")
        self.assertEqual(result.resends, 1)
        self.assertEqual(sent, ["enter"])
        self.assertIn({"result": "resend", "resends": 1}, events)
        self.assertTrue(any(e.get("result") == "confirmed" and e.get("signal") == "screen-working"
                            for e in events))

    def test_retry_exhaustion_raises(self):
        sent = []
        events = []

        with self.assertRaises(prompt_delivery.InjectDeliveryError):
            prompt_delivery.confirm_initial_prompt_delivered(
                "read TASK.md",
                lambda: "› read TASK.md gpt-5.5 · /ws/fresh",
                lambda: sent.append("enter"),
                log_event=lambda **fields: events.append(fields),
                check_delay_s=0,
                poll_s=0.01,
                timeout_s=0.05,
                resend_grace_s=0,
            )

        self.assertEqual(len(sent), prompt_delivery.TUI_DELIVERY_RETRIES)
        self.assertTrue(any(e.get("result") == "failed" for e in events))

    def test_no_double_enter_after_session_confirms(self):
        sent = []

        result = prompt_delivery.confirm_initial_prompt_delivered(
            "read TASK.md",
            lambda: "› read TASK.md gpt-5.5 · /ws/fresh",
            lambda: sent.append("enter"),
            turn_started=lambda: "session-user-turn",
            check_delay_s=0,
            poll_s=0.01,
            timeout_s=1,
            resend_grace_s=0,
        )

        self.assertEqual(result.signal, "session-user-turn")
        self.assertEqual(sent, [])

    def test_working_word_in_composer_does_not_confirm_turn(self):
        sent = []
        checks = {"n": 0}

        def turn_started():
            checks["n"] += 1
            return "session-user-turn" if checks["n"] >= 2 else None

        result = prompt_delivery.confirm_initial_prompt_delivered(
            "working TASK.md",
            lambda: "› working TASK.md gpt-5.5 · /ws/fresh",
            lambda: sent.append("enter"),
            turn_started=turn_started,
            check_delay_s=0,
            poll_s=0.01,
            timeout_s=1,
            resend_grace_s=0,
        )

        self.assertEqual(result.signal, "session-user-turn")
        self.assertEqual(sent, ["enter"])


if __name__ == "__main__":
    unittest.main()
