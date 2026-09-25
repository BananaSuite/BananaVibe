"""Per-task inference and public-network gateway. No workspace is mounted here."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import re
import select
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

import requests

MAX_BODY = 32 * 1024 * 1024
MAX_TUNNEL_BYTES = 512 * 1024 * 1024


def public_address(host, port):
    """Resolve once and pin connections to a public address, preventing DNS rebinding."""
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not results or any(not ipaddress.ip_address(row[4][0]).is_global for row in results):
        raise ValueError("Only public network addresses are allowed.")
    return results[0]


def connected_socket(host, port):
    family, kind, protocol, _, address = public_address(host, port)
    stream = socket.socket(family, kind, protocol)
    stream.settimeout(20)
    try:
        stream.connect(address)
    except BaseException:
        stream.close()
        raise
    return stream


def inference_request(config, path, body):
    """Limit the credential to inference with this task's selected model."""
    parsed = urlsplit(path)
    relative = unquote(parsed.path.removeprefix("/api"))
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/api/") or ".." in relative or "\\" in relative:
        raise ValueError("Unsupported inference endpoint.")
    allowed = {"/chat/completions", "/responses", "/messages", "/chat"}
    google = re.fullmatch(r"/models/(.+):(generateContent|streamGenerateContent)", relative)
    if relative not in allowed and not google:
        raise ValueError("Unsupported inference endpoint.")
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object.")
    if google:
        if google.group(1) != config["model"]:
            raise ValueError("This model is not enabled for the task.")
    elif body.get("model") != config["model"]:
        raise ValueError("This model is not enabled for the task.")
    if config.get("token_param") == "max_completion_tokens" and "max_tokens" in body:
        body["max_completion_tokens"] = body.pop("max_tokens")
    headers = {"Content-Type": "application/json"}
    package = config["package"]
    key = config.get("api_key", "")
    if package == "@ai-sdk/anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif package == "@ai-sdk/google":
        headers["x-goog-api-key"] = key
    elif package == "@ai-sdk/azure":
        headers["api-key"] = key
    elif key:
        headers["Authorization"] = "Bearer " + key
    query = parse_qs(parsed.query)
    query = {key: values[-1] for key, values in query.items() if key in {"alt", "api-version"}}
    suffix = "?" + urlencode(query) if query else ""
    return config["endpoint"].rstrip("/") + quote(relative, safe="/:@-") + suffix, headers, body


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(60)
        super().setup()

    def log_message(self, *_):
        pass

    def error(self, status, message):
        body = json.dumps({"error": {"message": message}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class InferenceHandler(Handler):
    def do_POST(self):
        config = self.server.config
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ") or self.headers.get("x-api-key", "") or self.headers.get("x-goog-api-key", "")
        if not supplied:
            supplied = parse_qs(urlsplit(self.path).query).get("key", [""])[0]
        if not hmac.compare_digest(supplied.encode(), config["token"].encode()):
            return self.error(401, "Invalid task credential.")
        with self.server.call_lock:
            if time.time() >= config["expires_at"] or self.server.calls >= config.get("max_calls", 1000):
                return self.error(429, "This task's runtime allowance has ended. Stop and resume it from BananaVibe.")
            self.server.calls += 1
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < size <= MAX_BODY:
                return self.error(413, "Inference request is too large or has no content length.")
            raw = self.rfile.read(size)
            if len(raw) != size:
                return self.error(400, "Incomplete request.")
            url, headers, body = inference_request(config, self.path, json.loads(raw))
        except (ValueError, UnicodeError):
            return self.error(400, "Unsupported inference request or model.")
        upstream = requests.Session()
        upstream.trust_env = False
        started = False
        try:
            with upstream.post(url, headers=headers, json=body, timeout=(15, 180), stream=True, allow_redirects=False) as response:
                if response.status_code >= 300:
                    return self.error(response.status_code if response.status_code < 600 else 502,
                                      f"The model provider returned {response.status_code}. Check the provider configuration or try later.")
                self.send_response(response.status_code)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                started = True
                for chunk in response.iter_content(chunk_size=128):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (requests.RequestException, OSError):
            if not started:
                try:
                    self.error(502, "The model provider could not be reached.")
                except OSError:
                    pass
            self.close_connection = True
        finally:
            upstream.close()


class ControlHandler(Handler):
    """Authenticated web-service access to the engine on its private network."""

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        supplied = self.headers.get("X-BananaVibe-Control", "")
        if not hmac.compare_digest(supplied.encode(), self.server.config["control_token"].encode()):
            return self.error(401, "Invalid control credential.")
        parsed = urlsplit(self.path)
        if (parsed.scheme or parsed.netloc or parsed.fragment or not self.path.startswith("/")
                or self.path.startswith("//") or len(self.path) > 8192):
            return self.error(400, "Invalid control path.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                return self.error(413, "Control request is too large.")
            body = self.rfile.read(size) if size else None
            if size and len(body) != size:
                return self.error(400, "Incomplete request.")
        except (ValueError, OSError):
            return self.error(400, "Invalid request body.")
        upstream = requests.Session()
        upstream.trust_env = False
        started = False
        try:
            with upstream.request(self.command, "http://agent:4096" + self.path, data=body,
                                  headers={"Authorization": self.headers.get("Authorization", ""), "Content-Type": "application/json"},
                                  timeout=(3, 60), stream=True, allow_redirects=False) as response:
                self.send_response(response.status_code)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                started = True
                total = 0
                for chunk in response.iter_content(65536):
                    total += len(chunk)
                    if total > 8 * 1024 * 1024:
                        break
                    self.wfile.write(chunk)
        except (requests.RequestException, OSError):
            if not started:
                try:
                    self.error(502, "The coding engine is starting or unavailable.")
                except OSError:
                    pass
        finally:
            self.close_connection = True
            upstream.close()


class EgressHandler(Handler):
    def do_CONNECT(self):
        parsed = urlsplit("//" + self.path)
        try:
            if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or parsed.port != 443:
                return self.error(403, "Only HTTPS tunnels to public addresses are allowed.")
            remote = connected_socket(parsed.hostname, parsed.port)
        except (ValueError, OSError):
            return self.error(403, "This network destination is not allowed or is unavailable.")
        with remote:
            self.send_response(200, "Connection established")
            self.end_headers()
            self.wfile.flush()
            self.relay(remote)

    def relay(self, remote):
        count = 0
        deadline = time.monotonic() + 600
        sockets = [self.connection, remote]
        while time.monotonic() < deadline and count < MAX_TUNNEL_BYTES:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                break
            for source in readable:
                try:
                    data = source.recv(65536)
                    if not data:
                        return
                    count += len(data)
                    target = remote if source is self.connection else self.connection
                    target.sendall(data)
                except OSError:
                    return
        self.close_connection = True

    def do_GET(self):
        self.forward_http()

    def do_HEAD(self):
        self.forward_http()

    def do_POST(self):
        self.forward_http()

    def forward_http(self):
        parsed = urlsplit(self.path)
        try:
            if parsed.scheme != "http" or parsed.username or parsed.password or parsed.fragment or parsed.port not in {None, 80}:
                return self.error(403, "Use HTTP port 80 or an HTTPS tunnel.")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                return self.error(413, "Request body is too large.")
            remote = connected_socket(parsed.hostname, 80)
        except (OSError, ValueError):
            return self.error(403, "This network destination is not allowed or is unavailable.")
        with remote:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            remote.sendall(f"{self.command} {path} HTTP/1.0\r\nHost: {parsed.netloc}\r\nConnection: close\r\n".encode())
            for key, value in self.headers.items():
                if key.lower() not in {"host", "connection", "proxy-connection", "proxy-authorization", "transfer-encoding", "keep-alive", "upgrade", "te", "trailer"}:
                    remote.sendall(f"{key}: {value}\r\n".encode())
            remote.sendall(b"\r\n")
            if size:
                remote.sendall(self.rfile.read(size))
            count = 0
            try:
                while count < MAX_TUNNEL_BYTES:
                    data = remote.recv(65536)
                    if not data:
                        break
                    count += len(data)
                    self.wfile.write(data)
            except OSError:
                pass
            self.close_connection = True


def main():
    with open(sys.argv[1]) as source:
        config = json.load(source)
    inference = BoundedServer(("0.0.0.0", 8000), InferenceHandler)
    inference.config = config
    inference.calls = 0
    inference.call_lock = threading.Lock()
    egress = BoundedServer(("0.0.0.0", 8080), EgressHandler)
    control = BoundedServer(("0.0.0.0", 8082), ControlHandler)
    control.config = config
    threading.Thread(target=egress.serve_forever, daemon=True).start()
    threading.Thread(target=control.serve_forever, daemon=True).start()
    inference.serve_forever()


if __name__ == "__main__":
    main()
