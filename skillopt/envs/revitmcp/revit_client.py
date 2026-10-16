"""Minimal JSON-RPC 2.0 client for the RevitMCP plugin's socket (127.0.0.1, one JSON object per message).

The plugin answers one request at a time per connection, in order, and echoes the request id with its JSON type.
This mirrors scripts/test/e2e_socket.mjs in the RevitMCP workspace.
"""

from __future__ import annotations

import codecs
import json
import socket
from typing import Any


class RevitRpcError(RuntimeError):
    """The plugin answered with a JSON-RPC error."""

    def __init__(self, method: str, error: dict):
        self.method = method
        self.error = error
        super().__init__(f"{method}: {error.get('code')} {error.get('message')}")


class RevitClient:
    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = 120.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buffer = ""
        self._next_id = 1
        self._decoder = json.JSONDecoder()
        self._utf8 = codecs.getincrementaldecoder("utf-8")()

    def connect(self) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        self._buffer = ""
        self._utf8.reset()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        """Send one request and return its `result`; raise RevitRpcError on an error reply."""
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": request_id}
        self._sock.settimeout(timeout or self.timeout)
        self._sock.sendall(json.dumps(message).encode("utf-8"))
        reply = self._read_message()
        if "error" in reply and reply["error"] is not None:
            raise RevitRpcError(method, reply["error"])
        return reply.get("result")

    def _read_message(self) -> dict:
        assert self._sock is not None
        while True:
            text = self._buffer.lstrip()
            if text:
                try:
                    value, end = self._decoder.raw_decode(text)
                except json.JSONDecodeError:
                    pass
                else:
                    self._buffer = text[end:]
                    return value
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("the Revit plugin closed the connection")
            self._buffer += self._utf8.decode(chunk)
