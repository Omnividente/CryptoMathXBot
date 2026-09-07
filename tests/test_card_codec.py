"""Pure stdlib checks: also runnable without Telegram or a network connection."""

import unittest

from cryptomathxbot.card import CardAction, CardSigner, read_request


class CardCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signer = CardSigner("unit-test-card-key")
        self.expression = "(0.123456789 BTC + 2 ETH) / 3"
        self.token = self.signer.sign(self.expression, 42, -100, 17)

    def test_new_signer_after_restart_verifies_exact_request(self) -> None:
        restored = CardSigner("unit-test-card-key")
        self.assertTrue(restored.verify(self.token, self.expression, 42, -100, 17))

    def test_another_user_cannot_use_card(self) -> None:
        self.assertFalse(self.signer.verify(self.token, self.expression, 43, -100, 17))

    def test_another_chat_cannot_use_card(self) -> None:
        self.assertFalse(self.signer.verify(self.token, self.expression, 42, -101, 17))

    def test_another_topic_cannot_use_card(self) -> None:
        for topic in (None, 18):
            with self.subTest(topic=topic):
                self.assertFalse(self.signer.verify(self.token, self.expression, 42, -100, topic))

    def test_rounded_or_changed_expression_is_rejected(self) -> None:
        altered = self.expression.replace("0.123456789", "0.12")
        self.assertFalse(self.signer.verify(self.token, altered, 42, -100, 17))

    def test_credential_rotation_invalidates_old_signature(self) -> None:
        self.assertFalse(CardSigner("rotated-test-key").verify(self.token, self.expression, 42, -100, 17))

    def test_bad_signatures_are_rejected(self) -> None:
        for value in ("", self.token + "x", "?" * 22, "я" * 22):
            with self.subTest(value=value):
                self.assertFalse(self.signer.verify(value, self.expression, 42, -100, 17))

    def test_all_actions_fit_telegram_callback_limit(self) -> None:
        pairs = [("refresh", view) for view in ("text", "1h", "24h", "7d")]
        pairs += [("chart", view) for view in ("1h", "24h", "7d")]
        pairs += [("text", "text"), ("close", "text")]
        for action, view in pairs:
            with self.subTest(action=action, view=view):
                item = CardAction(self.token, action, view)
                encoded = item.encode()
                self.assertLessEqual(len(encoded.encode("utf-8")), 64)
                self.assertEqual(CardAction.parse(encoded), item)

    def test_unknown_actions_and_extra_fields_are_rejected(self) -> None:
        values = [
            f"r|{self.token}|chart|text", f"r|{self.token}|close|1h",
            f"r|{self.token}|delete|text", f"r|{self.token}|refresh|1y",
            f"r|{self.token}|refresh|text|extra", "x" * 65,
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                CardAction.parse(value)

    def test_request_is_read_only_from_first_line(self) -> None:
        self.assertEqual(read_request("Запрос: BTC\n\nИтого: $100"), "BTC")
        self.assertIsNone(read_request("Итого: $100\nЗапрос: BTC\n"))
        self.assertIsNone(read_request("Запрос: BTC"))
        self.assertIsNone(read_request(None))

    def test_request_limit_does_not_truncate_precision(self) -> None:
        expression = "1" * 496 + " BTC"
        token = self.signer.sign(expression, 42, 42, None)
        restored = read_request("Запрос: " + expression + "\n\nbody")
        self.assertEqual(restored, expression)
        self.assertTrue(self.signer.verify(token, expression, 42, 42, None))
        with self.assertRaises(ValueError):
            self.signer.sign(expression + "x", 42, 42, None)

    def test_control_characters_and_empty_requests_are_rejected(self) -> None:
        for expression in ("", "BTC\nETH", "BTC\r", "BTC\tETH"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                self.signer.sign(expression, 42, 42, None)

    def test_empty_credential_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CardSigner("")


if __name__ == "__main__":
    unittest.main()
