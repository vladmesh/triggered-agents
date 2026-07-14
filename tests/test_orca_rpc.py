"""Unit tests for the Orca runtime RPC client."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from triggered_agents.runtime import orca_rpc


class _FakeSocket:
    def __init__(self, chunks: list[bytes]):
        self.chunks = list(chunks)
        self.sent = b""
        self.closed = False
        self.timeout = None
        self.endpoint = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, endpoint):
        self.endpoint = endpoint

    def sendall(self, data):
        self.sent += data

    def recv(self, _size):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class OrcaRpcEncodingTest(unittest.TestCase):
    def test_call_handles_utf8_split_across_socket_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            meta = Path(td) / "orca-runtime.json"
            meta.write_text(
                json.dumps({"transports": [{"kind": "unix", "endpoint": "/tmp/orca.sock"}], "authToken": "tok"})
            )
            response = (
                json.dumps(
                    {"id": "request-1", "ok": True, "result": {"text": "привет"}},
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            split = response.index("п".encode("utf-8")) + 1
            fake = _FakeSocket([response[:split], response[split:]])

            with (
                mock.patch.object(orca_rpc, "_META_PATH", meta),
                mock.patch.object(orca_rpc.uuid, "uuid4", return_value="request-1"),
                mock.patch.object(orca_rpc.socket, "socket", return_value=fake),
            ):
                result = orca_rpc.call("session.tabs.listAll", timeout=1.0)

            self.assertEqual(result["result"]["text"], "привет")
            self.assertTrue(fake.closed)
            self.assertIn(b"session.tabs.listAll", fake.sent)


if __name__ == "__main__":
    unittest.main()
