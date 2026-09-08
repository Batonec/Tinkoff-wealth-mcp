"""Transport tests against a local TLS gRPC server (need grpcio, the Tinkoff SDK, openssl).

They exercise the two knobs that matter when the real API misbehaves:

* an explicit root-CA bundle (``ca_bundle`` / ``TINKOFF_INVEST_CA_BUNDLE``) — the fix
  for the Минцифры certificate the API moved to in August 2026;
* the per-RPC deadline (``timeout_seconds``), so a hung API fails fast instead of
  blocking a tool call past the MCP client's timeout.

Skipped automatically when the SDK is not installed (CI); run locally with the
``[tinkoff]`` extras plus the SDK (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from concurrent import futures

try:
    import grpc
    from tinkoff.invest.exceptions import RequestError
    from tinkoff.invest.grpc import users_pb2, users_pb2_grpc

    HAVE_SDK = True
except ImportError:  # pragma: no cover - CI has no SDK
    HAVE_SDK = False

from investor_mcp.adapters import TinkoffInvestAdapter
from investor_mcp.errors import classify_broker_error, describe_error, error_hint


def _self_signed_cert(directory: str) -> tuple[str, str]:
    key = os.path.join(directory, "key.pem")
    cert = os.path.join(directory, "cert.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-keyout", key, "-out", cert, "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    return key, cert


if HAVE_SDK:

    class _Users(users_pb2_grpc.UsersServiceServicer):
        def __init__(self, delay: float = 0.0) -> None:
            self.delay = delay

        def GetAccounts(self, request, context):  # noqa: N802 - gRPC naming
            time.sleep(self.delay)
            account = users_pb2.Account(
                id="acc-1",
                name="Local",
                type=users_pb2.ACCOUNT_TYPE_TINKOFF,
                status=users_pb2.ACCOUNT_STATUS_OPEN,
            )
            return users_pb2.GetAccountsResponse(accounts=[account])


@unittest.skipUnless(HAVE_SDK and shutil.which("openssl"), "needs grpcio, the Tinkoff SDK and openssl")
class TransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        key, self.cert = _self_signed_cert(self.tmp)
        with open(key, "rb") as key_file, open(self.cert, "rb") as cert_file:
            self.credentials = grpc.ssl_server_credentials([(key_file.read(), cert_file.read())])
        self.servers: list = []

    def tearDown(self) -> None:
        for server in self.servers:
            server.stop(0)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _serve(self, delay: float = 0.0) -> str:
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        users_pb2_grpc.add_UsersServiceServicer_to_server(_Users(delay), server)
        port = server.add_secure_port("localhost:0", self.credentials)
        server.start()
        self.servers.append(server)
        return f"localhost:{port}"

    def test_ca_bundle_makes_private_ca_trusted(self) -> None:
        target = self._serve()
        adapter = TinkoffInvestAdapter("t", target=target, ca_bundle=self.cert, timeout_seconds=5)

        accounts = adapter.list_accounts()

        self.assertEqual([account.account_id for account in accounts], ["acc-1"])
        self.assertEqual(accounts[0].type, "brokerage")

    def test_default_roots_reject_private_ca_with_tls_hint(self) -> None:
        target = self._serve()
        adapter = TinkoffInvestAdapter("t", target=target, timeout_seconds=5)  # gRPC default roots

        with self.assertRaises(RequestError) as ctx:
            adapter.list_accounts()

        text = describe_error(ctx.exception)
        self.assertIn("UNAVAILABLE", text)
        self.assertIn("Минцифры", error_hint(text) or "")

    def test_deadline_bounds_a_hung_call(self) -> None:
        target = self._serve(delay=3.0)
        adapter = TinkoffInvestAdapter("t", target=target, ca_bundle=self.cert, timeout_seconds=0.5)
        started = time.monotonic()

        with self.assertRaises(RequestError) as ctx:
            adapter.list_accounts()

        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(ctx.exception.code, grpc.StatusCode.DEADLINE_EXCEEDED)
        self.assertEqual(classify_broker_error(ctx.exception), "BROKER_API_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
