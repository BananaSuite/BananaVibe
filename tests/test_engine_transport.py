"""Transient engine reads may recover; a task submission must not be duplicated."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from bananavibe.engine import DockerEngine, EngineUnavailable, Interrupted


class API(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def call(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.calls += 1
        status = 503 if self.server.calls == 1 else 200
        body = b'{"healthy": true}'
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = call


@pytest.fixture
def engine(monkeypatch):
    server = ThreadingHTTPServer(('127.0.0.1', 0), API)
    server.calls = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine = object.__new__(DockerEngine)
    engine.port, engine.server_secret, engine.control_secret = server.server_port, 'fixture', 'fixture-control'
    engine.pulse = lambda *_: None
    monkeypatch.setattr('bananavibe.engine.time.sleep', lambda *_: None)
    yield engine, server
    server.shutdown()
    server.server_close()
    thread.join()


def test_engine_read_recovers_without_resubmitting_work(engine):
    client, server = engine
    assert client.request('GET', '/session/status')['healthy']
    assert server.calls == 2


def test_engine_task_post_is_never_blindly_retried(engine):
    client, server = engine
    with pytest.raises(EngineUnavailable):
        client.request('POST', '/session/task/prompt_async', {'parts': []})
    assert server.calls == 1


def test_stop_is_observed_between_engine_retries(engine):
    client, server = engine
    def stopped(*_):
        raise Interrupted('A maintainer stopped the task')
    client.pulse = stopped
    with pytest.raises(Interrupted):
        client.request('GET', '/session/status')
    assert server.calls == 1
