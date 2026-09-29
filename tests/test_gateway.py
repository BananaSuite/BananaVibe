"""The sandbox gateway: credential scoping, egress policy and control relay."""

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

import pytest

from sandbox.gateway import (ControlHandler, EgressHandler, InferenceHandler, Server, host_allowed,
                             inference_request, public_address)


def provider(**changes):
    return {"endpoint": "https://models.example/v1", "api_key": "real-secret", "model": "code-model",
            "provider": "openai-compatible", **changes}


def test_fixed_upstream_and_scoped_model():
    url, headers, _ = inference_request(provider(), "/api/chat/completions", {"model": "code-model"})
    assert url == "https://models.example/v1/chat/completions"
    assert headers["Authorization"] == "Bearer real-secret"
    with pytest.raises(ValueError, match="not enabled"):
        inference_request(provider(), "/api/chat/completions", {"model": "expensive-model"})


@pytest.mark.parametrize("path", ["/api/files", "/api/../models", "/api/%2e%2e/chat/completions",
                                  "/api//evil.example/chat/completions", "https://evil.example/api/chat/completions",
                                  "/api/organizations", "/api/models/other:generateContent", "/chat/completions"])
def test_non_inference_paths_are_refused(path):
    with pytest.raises(ValueError):
        inference_request(provider(), path, {"model": "code-model"})


def test_provider_authentication_and_parameters():
    _, headers, _ = inference_request(provider(provider="anthropic"), "/api/messages", {"model": "code-model"},
                                      {"anthropic-beta": "tools-2024", "x-api-key": "task-key", "cookie": "c"})
    assert headers["x-api-key"] == "real-secret" and headers["anthropic-version"] == "2023-06-01"
    assert headers["anthropic-beta"] == "tools-2024" and "cookie" not in headers
    url, headers, _ = inference_request(provider(provider="google"),
                                        "/api/models/code-model:streamGenerateContent?key=task&alt=sse", {})
    assert headers["x-goog-api-key"] == "real-secret" and url.endswith("?alt=sse") and "task" not in url
    _, headers, _ = inference_request(provider(provider="azure"), "/api/chat/completions", {"model": "code-model"})
    assert headers["api-key"] == "real-secret"
    _, _, body = inference_request(provider(token_param="max_completion_tokens"), "/api/chat/completions",
                                   {"model": "code-model", "max_tokens": 10})
    assert body == {"model": "code-model", "max_completion_tokens": 10}


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "172.17.0.1", "192.168.1.1",
                                     "::1", "fc00::1", "0.0.0.0", "::ffff:127.0.0.1", "100.64.0.1"])
def test_private_destinations_are_refused(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_, **__: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(ValueError, match="public"):
        public_address("attacker.example", 443)


def test_mixed_dns_answers_are_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_, **__: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in ("8.8.8.8", "127.0.0.1")])
    with pytest.raises(ValueError):
        public_address("rebinding.example", 443)


def test_host_patterns():
    assert host_allowed("files.pythonhosted.org", ["*.pythonhosted.org"])
    assert not host_allowed("pythonhosted.org.evil", ["*.pythonhosted.org"])
    assert not host_allowed("", ["*"])


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.received = (self.path, dict(self.headers), data)
        code = self.server.code
        payload = (b'{"error": "bad key real-secret"}' if code >= 300 else b'data: {"ok": true}\n\n')
        self.send_response(code)
        self.send_header("Content-Type", "text/event-stream" if code < 300 else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def upstream():
    server = serve(ThreadingHTTPServer(("127.0.0.1", 0), Upstream))
    server.code = 200
    yield server
    server.shutdown()
    server.server_close()


def call(server, method="POST", path="/api/chat/completions", headers=None, body=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request(method, path, json.dumps(body if body is not None else {"model": "code"}),
                       {"Content-Type": "application/json", **(headers or {})})
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


@pytest.fixture
def inference(upstream):
    config = {"endpoint": f"http://127.0.0.1:{upstream.server_port}/v1", "api_key": "real-secret", "token": "task-key",
              "expires_at": time.time() + 60, "model": "code", "provider": "openai-compatible", "max_calls": 3}
    server = serve(Server(("127.0.0.1", 0), InferenceHandler, config))
    yield server
    server.shutdown()
    server.server_close()


def test_inference_swaps_credentials_and_streams(inference, upstream):
    status, body = call(inference, headers={"Authorization": "Bearer task-key"})
    assert status == 200 and body == b'data: {"ok": true}\n\n'
    path, headers, _ = upstream.received
    assert path == "/v1/chat/completions" and headers["Authorization"] == "Bearer real-secret"
    assert "task-key" not in json.dumps(headers)


def test_provider_errors_pass_through_with_the_key_redacted(inference, upstream):
    upstream.code = 400
    status, body = call(inference, headers={"Authorization": "Bearer task-key"})
    assert status == 400 and b"real-secret" not in body and b"[redacted]" in body


def test_inference_rejects_bad_tokens_models_and_spent_budgets(inference):
    assert call(inference, headers={"Authorization": "Bearer other"})[0] == 401
    assert call(inference, headers={"Authorization": "Bearer task-key"}, body={"model": "unapproved"})[0] == 400
    call(inference, headers={"Authorization": "Bearer task-key"})
    call(inference, headers={"Authorization": "Bearer task-key"})
    assert call(inference, headers={"Authorization": "Bearer task-key"})[0] == 429


def test_control_requires_its_own_secret(upstream, monkeypatch):
    original = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host == "agent":
            return original("127.0.0.1", upstream.server_port, *args, **kwargs)
        return original(host, port, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    server = serve(Server(("127.0.0.1", 0), ControlHandler, {"control_token": "control-secret"}))
    try:
        assert call(server, path="/global/health", headers={"X-BananaVibe-Control": "task-key"})[0] == 401
        assert call(server, path="http://evil.example/x", headers={"X-BananaVibe-Control": "control-secret"})[0] == 400
        assert call(server, "GET", "/global/health", headers={"X-BananaVibe-Control": "control-secret",
                                                              "Authorization": "Basic abc"})[0] == 200
        path, headers, _ = upstream.received
        assert path == "/global/health" and headers["Authorization"] == "Basic abc"
        assert "control-secret" not in json.dumps(headers)
    finally:
        server.shutdown()
        server.server_close()


def status_line(server, request):
    with socket.create_connection(("127.0.0.1", server.server_port), 5) as connection:
        connection.sendall(request)
        return connection.recv(256).split(b"\r\n")[0]


@pytest.fixture
def egress():
    server = serve(Server(("127.0.0.1", 0), EgressHandler, {"egress": ["*.allowed.example", "localhost"]}))
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("target", ["127.0.0.1:443", "[::1]:443", "10.0.0.1:443", "169.254.169.254:443",
                                    "localhost:443", "evil.example:443", "x.allowed.example:22"])
def test_tunnels_outside_policy_are_refused(egress, target):
    assert b"403" in status_line(egress, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())


def test_plain_http_follows_the_same_policy(egress):
    assert b"403" in status_line(egress, b"GET http://169.254.169.254/latest/ HTTP/1.1\r\nHost: x\r\n\r\n")
    assert b"403" in status_line(egress, b"GET https://x.allowed.example/ HTTP/1.1\r\nHost: x\r\n\r\n")
    assert b"403" in status_line(egress, b"GET http://evil.example/ HTTP/1.1\r\nHost: x\r\n\r\n")
