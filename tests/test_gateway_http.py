"""Exercise credential boundaries over HTTP, including upstream failures."""
import json
import socket
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sandbox.gateway import BoundedServer, ControlHandler, EgressHandler, InferenceHandler


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.received = (self.path, dict(self.headers), data)
        code = self.server.code
        payload = b'provider-secret-must-not-leak' if code >= 300 else b'{"ok": true}'
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    server.code = 200
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(server, method='POST', path='/api/chat/completions', token='task-key', body=None, control=False):
    conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    headers = {'Content-Type': 'application/json'}
    headers['X-BananaVibe-Control' if control else 'Authorization'] = token if control else 'Bearer ' + token
    conn.request(method, path, json.dumps(body or {'model': 'code', 'messages': []}), headers)
    response = conn.getresponse()
    result = response.status, response.read()
    conn.close()
    return result


@pytest.fixture
def inference(upstream):
    server = BoundedServer(('127.0.0.1', 0), InferenceHandler)
    server.config = {'endpoint': f'http://127.0.0.1:{upstream.server_port}/v1', 'api_key': 'provider-secret', 'token': 'task-key', 'expires_at': time.time() + 60, 'model': 'code', 'package': '@ai-sdk/openai-compatible'}
    server.calls = 0
    server.call_lock = threading.Lock()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_inference_replaces_task_credential_and_hides_upstream_errors(inference, upstream):
    assert call(inference)[0] == 200
    path, headers, data = upstream.received
    assert path == '/v1/chat/completions'
    assert headers['Authorization'] == 'Bearer provider-secret'
    assert 'task-key' not in str(headers)
    upstream.code = 403
    status, response = call(inference)
    assert status == 403
    assert b'provider-secret' not in response


def test_inference_rejects_other_task_credentials_expiry_and_models(inference):
    assert call(inference, token='other-task')[0] == 401
    assert call(inference, body={'model': 'expensive-unapproved-model'})[0] == 400
    inference.config['expires_at'] = 1
    assert call(inference)[0] == 429


def test_control_requires_separate_secret_and_only_reaches_its_engine(upstream, monkeypatch):
    original = socket.getaddrinfo
    def resolve(host, port, *args, **kwargs):
        if host == 'agent':
            return original('127.0.0.1', upstream.server_port, *args, **kwargs)
        return original(host, port, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    server = BoundedServer(('127.0.0.1', 0), ControlHandler)
    server.config = {'control_token': 'web-control-key'}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert call(server, path='/global/health', token='task-key', control=True)[0] == 401
        assert call(server, path='http://attacker.example/global/health', token='web-control-key', control=True)[0] == 400
        assert call(server, method='GET', path='/global/health', token='web-control-key', control=True)[0] == 200
        path, headers, body = upstream.received
        assert path == '/global/health'
        assert 'X-BananaVibe-Control' not in headers
        assert 'web-control-key' not in str(headers)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def egress():
    server = BoundedServer(('127.0.0.1', 0), EgressHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def status_line(server, request):
    conn = socket.create_connection(('127.0.0.1', server.server_port), 5)
    with conn:
        conn.sendall(request)
        return conn.recv(256).split(b'\r\n')[0]


@pytest.mark.parametrize('target', ['127.0.0.1:443', '[::1]:443', '10.0.0.1:443', '192.168.1.1:443',
                                    '172.17.0.1:443', '169.254.169.254:443', 'localhost:443'])
def test_tunnels_to_the_host_and_its_networks_are_refused(egress, target):
    line = status_line(egress, f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode())
    assert b'403' in line


def test_only_https_tunnels_and_public_http_are_proxied(egress):
    assert b'403' in status_line(egress, b'CONNECT example.com:22 HTTP/1.1\r\nHost: example.com\r\n\r\n')
    assert b'403' in status_line(egress, b'GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: x\r\n\r\n')
    assert b'403' in status_line(egress, b'GET https://example.com/ HTTP/1.1\r\nHost: x\r\n\r\n')
    assert b'403' in status_line(egress, b'POST http://10.0.0.1:8080/ HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n')
