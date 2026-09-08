from __future__ import annotations

import unittest
from types import SimpleNamespace

from investor_mcp.errors import classify_broker_error, describe_error, error_hint


class SdkLikeError(Exception):
    """Shaped like tinkoff.invest.RequestError: code/details attributes, empty args."""

    def __init__(self, code: str, details: str) -> None:
        super().__init__()
        self.code = SimpleNamespace(name=code)
        self.details = details
        self.metadata = None


class RpcLikeError(Exception):
    """Shaped like a raw grpc.RpcError: code()/details() are methods."""

    def code(self):
        return SimpleNamespace(name="DEADLINE_EXCEEDED")

    def details(self):
        return "Deadline Exceeded"


class ErrorHelpersTest(unittest.TestCase):
    def test_describe_reads_grpc_code_and_details(self) -> None:
        exc = SdkLikeError(
            "UNAVAILABLE",
            "failed to connect to all addresses; last error: UNKNOWN: ipv4:1.2.3.4:443: "
            "Ssl handshake failed: CERTIFICATE_VERIFY_FAILED",
        )
        text = describe_error(exc)
        self.assertTrue(text.startswith("UNAVAILABLE: failed to connect"))
        self.assertIn("CERTIFICATE_VERIFY_FAILED", text)

    def test_describe_plain_exceptions(self) -> None:
        self.assertEqual(describe_error(RuntimeError("broker down")), "broker down")
        self.assertEqual(describe_error(RuntimeError()), "RuntimeError")

    def test_describe_handles_callable_code_and_details(self) -> None:
        self.assertEqual(describe_error(RpcLikeError()), "DEADLINE_EXCEEDED: Deadline Exceeded")

    def test_classify(self) -> None:
        self.assertEqual(classify_broker_error(SdkLikeError("UNAUTHENTICATED", "40003")), "BROKER_AUTH_FAILED")
        self.assertEqual(classify_broker_error(SdkLikeError("PERMISSION_DENIED", "x")), "BROKER_AUTH_FAILED")
        self.assertEqual(classify_broker_error(SdkLikeError("RESOURCE_EXHAUSTED", "30042")), "BROKER_RATE_LIMIT")
        self.assertEqual(classify_broker_error(SdkLikeError("UNAVAILABLE", "x")), "BROKER_API_UNAVAILABLE")
        self.assertEqual(classify_broker_error(RuntimeError("x")), "BROKER_API_UNAVAILABLE")

    def test_hints(self) -> None:
        tls = error_hint("UNAVAILABLE: failed to connect to all addresses; Ssl handshake failed: CERTIFICATE_VERIFY_FAILED")
        self.assertIn("Минцифры", tls)
        self.assertIn("TINKOFF_INVEST_CA_BUNDLE", tls)
        self.assertIn("TINKOFF_INVEST_TOKEN", error_hint("UNAUTHENTICATED: 40003 Token is invalid"))
        self.assertIn("лимит", error_hint("RESOURCE_EXHAUSTED: 30042"))
        self.assertIn("journalctl", error_hint("DEADLINE_EXCEEDED: Deadline Exceeded"))
        self.assertIn("journalctl", error_hint("UNAVAILABLE: failed to connect to all addresses"))
        self.assertIsNone(error_hint("KeyError: 'x'"))


if __name__ == "__main__":
    unittest.main()
