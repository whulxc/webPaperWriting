"""Bounded, direct loopback client for the pinned WebCodex MCP HTTP API."""
from __future__ import annotations

import errno
import http.client
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class NativeError(RuntimeError):
    def __init__(self, message, *, code="NATIVE_API_ERROR", **details):
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise NativeError("Native API redirect refused", code="NATIVE_REDIRECT_REFUSED")


class NativeClient:
    def __init__(self, server_url: str, token_file: str | Path,
                 timeout: float = 30, max_bytes: int = 8 * 1024 * 1024):
        url = urllib.parse.urlsplit(server_url)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or url.username or url.password or url.query or url.fragment
                or url.path not in {"", "/", "/mcp"}):
            raise ValueError("Native API requires an explicit loopback HTTP endpoint")
        self.url = server_url.rstrip("/")
        if not self.url.endswith("/mcp"):
            self.url += "/mcp"
        self.token_file = Path(token_file)
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self.sequence = 0

    def _token(self):
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise NativeError("Native credentials require a POSIX Gateway host. On Windows, run it in WSL 2.",
                              code="PLATFORM_UNSUPPORTED")
        flags = os.O_RDONLY | os.O_NOFOLLOW
        fd = os.open(self.token_file, flags)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.getuid() or info.st_size > 16384):
                raise ValueError("Credential reference must be an owned private regular file")
            token = stream.read().strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError("Invalid credential file")
        return token

    def request(self, method: str, params: dict | None = None):
        self.sequence += 1
        params = dict(params or {})
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + self._token()}
        # Native stateless artifact transport requires these protocol observations.
        if method == "resources/read" or (method == "tools/call" and params.get("name") == "project_artifact"):
            params["_meta"] = {**params.get("_meta", {}),
                               "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                               "io.modelcontextprotocol/clientCapabilities": {}}
            headers.update({"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method,
                            "Mcp-Name": params.get("name", params.get("uri", ""))})
        payload = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers)
        started = time.monotonic()
        phase = "awaiting_response"

        def failure(code, message, **extra):
            details = {"request_id": payload["id"], "phase": phase,
                       "elapsed_seconds": round(time.monotonic() - started, 6), **extra}
            # Only protocol names enter diagnostics. Arguments and resource URIs
            # may contain private information and are never included.
            if isinstance(method, str) and re.fullmatch(r"[A-Za-z0-9_/.-]{1,128}", method):
                details["rpc_method"] = method
            name = params.get("name") if method == "tools/call" else None
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
                details["tool"] = name
            return NativeError(message, code=code, **details)

        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                phase = "reading_response"
                raw = response.read(self.max_bytes + 1)
        except urllib.error.HTTPError as error:
            raise failure("NATIVE_HTTP_ERROR", f"Native API HTTP {error.code}",
                          http_status=error.code) from None
        except NativeError as error:
            raise failure(error.code, error.message) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            if isinstance(reason, TimeoutError):
                # urllib.open covers both connecting and waiting for response
                # headers. The phase records what is known without guessing.
                raise failure("NATIVE_TIMEOUT", "Native API request timed out") from None
            if isinstance(reason, ConnectionRefusedError) or getattr(reason, "errno", None) == errno.ECONNREFUSED:
                raise failure("NATIVE_CONNECTION_REFUSED", "Native API connection refused") from None
            raise failure("NATIVE_CONNECTION_ERROR", "Native API connection failed") from None
        phase = "decoding_response"
        if len(raw) > self.max_bytes:
            raise failure("NATIVE_RESPONSE_TOO_LARGE", "Native response exceeds configured byte limit")
        try:
            text = raw.decode("utf-8")
            if text.lstrip().startswith("{"):
                reply = json.loads(text)
            else:
                events = [json.loads(line[5:].strip()) for line in text.splitlines()
                          if line.startswith("data:") and line[5:].strip().startswith("{")]
                reply = next(event for event in reversed(events)
                             if isinstance(event, dict) and event.get("id") == payload["id"])
        except (ValueError, StopIteration):
            raise failure("NATIVE_INVALID_RESPONSE", "Native API returned invalid JSON-RPC") from None
        if not isinstance(reply, dict):
            raise failure("NATIVE_INVALID_RESPONSE", "Native API returned invalid JSON-RPC")
        if reply.get("id") != payload["id"] or "error" in reply:
            raise failure("NATIVE_RPC_ERROR", "Native JSON-RPC request failed")
        return reply.get("result", {})

    def list_tools(self):
        return self.request("tools/list", {})

    def call(self, name: str, arguments: dict):
        return self.request("tools/call", {"name": name, "arguments": arguments})
