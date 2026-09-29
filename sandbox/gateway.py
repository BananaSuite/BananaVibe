"""Per-task gateway: the sandbox's only way out. Standard library only.

:8000  inference  Accepts the task-scoped token, checks the path and model,
                  and forwards to the configured provider with the real key.
:8080  egress     HTTP proxy for public hosts that match the allowlist.
                  Private, loopback, link-local and metadata addresses are
                  always refused, and DNS is resolved once (no rebinding).
:8082  control    Authenticated relay from the controller to OpenCode.
"""

import fnmatch
import hmac
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import re
import select
import socket
import ssl
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

MAX_BODY = 32 * 1024 * 1024
MAX_TUNNEL = 1024 * 1024 * 1024
MAX_CONTROL_RESPONSE = 16 * 1024 * 1024
INFERENCE_PATHS = {"/chat/completions", "/responses", "/messages", "/chat"}
FORWARDED = {"anthropic-version", "anthropic-beta", "accept", "openai-beta"}


def host_allowed(host, patterns):
    host = (host or "").lower().rstrip(".")
    return bool(host) and any(pattern == "*" or host == pattern
                              or (pattern.startswith("*.") and fnmatch.fnmatchcase(host, pattern))
                              for pattern in patterns)


def public_address(host, port):
    """Resolve once and require every answer to be a public address."""
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not results or any(not ipaddress.ip_address(row[4][0].split("%")[0]).is_global for row in results):
        raise ValueError("Only public network addresses are allowed.")
    return results[0]


def connect_public(host, port):
    family, kind, protocol, _, address = public_address(host, port)
    stream = socket.socket(family, kind, protocol)
    stream.settimeout(20)
    try:
        stream.connect(address)
    except BaseException:
        stream.close()
        raise
    return stream


def inference_request(config, path, body, client_headers=None):
    """Map a sandbox request onto the provider, limited to this task's model."""
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/api/"):
        raise ValueError("Unsupported inference endpoint.")
    relative = unquote(parsed.path[len("/api"):])
    if ".." in relative or "\\" in relative or "//" in relative:
        raise ValueError("Unsupported inference endpoint.")
    google = re.fullmatch(r"/models/([^/]+):(generateContent|streamGenerateContent)", relative)
    if relative not in INFERENCE_PATHS and not google:
        raise ValueError("Unsupported inference endpoint.")
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object.")
    requested = google.group(1) if google else body.get("model")
    if requested != config["model"]:
        raise ValueError("This model is not enabled for the task.")
    if config.get("token_param") == "max_completion_tokens" and "max_tokens" in body:
        body["max_completion_tokens"] = body.pop("max_tokens")
    headers = {"Content-Type": "application/json"}
    for name, value in (client_headers or {}).items():
        if name.lower() in FORWARDED:
            headers[name.lower()] = value
    key, provider = config.get("api_key", ""), config["provider"]
    if provider == "anthropic":
        headers["x-api-key"] = key
        headers.setdefault("anthropic-version", "2023-06-01")
    elif provider == "google":
        headers["x-goog-api-key"] = key
    elif provider == "azure":
        headers["api-key"] = key
    elif key:
        headers["Authorization"] = "Bearer " + key
    query = {name: values[-1] for name, values in parse_qs(parsed.query).items() if name in {"alt", "api-version"}}
    url = config["endpoint"].rstrip("/") + quote(relative, safe="/:@-.") + ("?" + urlencode(query) if query else "")
    return url, headers, body


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, handler, config):
        self.config = config
        self.slots = threading.BoundedSemaphore(48)
        self.calls = 0
        self.call_lock = threading.Lock()
        super().__init__(address, handler)

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
        self.request.settimeout(120)
        super().setup()

    def log_message(self, *_):
        pass

    def fail(self, status, message):
        body = json.dumps({"error": {"message": message, "type": "bananavibe_gateway"}}).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
        self.close_connection = True

    def body(self, required):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Chunked request bodies are not supported.")
        size = int(self.headers.get("Content-Length") or "0")
        if not (1 if required else 0) <= size <= MAX_BODY:
            raise ValueError("The request body is missing or too large.")
        raw = self.rfile.read(size) if size else b""
        if len(raw) != size:
            raise ValueError("Incomplete request body.")
        return raw


def open_upstream(url, timeout):
    parsed = urlsplit(url)
    if parsed.scheme == "https":
        connection = HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout,
                                     context=ssl.create_default_context())
    else:
        connection = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
    return connection, (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")


class InferenceHandler(Handler):
    def do_POST(self):
        config = self.server.config
        supplied = (self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                    or self.headers.get("x-api-key", "") or self.headers.get("x-goog-api-key", "")
                    or self.headers.get("api-key", "") or parse_qs(urlsplit(self.path).query).get("key", [""])[0])
        if not hmac.compare_digest(supplied.encode(), config["token"].encode()):
            return self.fail(401, "Invalid task credential.")
        with self.server.call_lock:
            if time.time() >= config["expires_at"] or self.server.calls >= config.get("max_calls", 1000):
                return self.fail(429, "This task's model allowance is used up. Resume the task from the issue.")
            self.server.calls += 1
        try:
            url, headers, body = inference_request(config, self.path, json.loads(self.body(True)), dict(self.headers))
        except (ValueError, UnicodeError) as error:
            return self.fail(400, str(error))
        raw = json.dumps(body).encode()
        headers["Content-Length"] = str(len(raw))
        connection, target = open_upstream(url, 300)
        started = False
        try:
            connection.request("POST", target, body=raw, headers=headers)
            response = connection.getresponse()
            if response.status >= 300:
                # Pass the provider's error through (its SDK understands it),
                # with the real key removed in case the provider echoed it.
                detail = response.read(16384)
                key = config.get("api_key", "").encode()
                if key:
                    detail = detail.replace(key, b"[redacted]")
                self.send_response(response.status if 300 <= response.status < 600 else 502)
                self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(detail)))
                self.end_headers()
                self.wfile.write(detail)
                return
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            started = True
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, HTTPException):
            if not started:
                self.fail(502, "The model provider could not be reached.")
        finally:
            self.close_connection = True
            connection.close()


class ControlHandler(Handler):
    def do_GET(self):
        self.relay()

    def do_POST(self):
        self.relay()

    def relay(self):
        if not hmac.compare_digest(self.headers.get("X-BananaVibe-Control", "").encode(),
                                   self.server.config["control_token"].encode()):
            return self.fail(401, "Invalid control credential.")
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or not self.path.startswith("/") or self.path.startswith("//") \
                or len(self.path) > 8192:
            return self.fail(400, "Invalid control path.")
        try:
            raw = self.body(False)
        except (ValueError, OSError) as error:
            return self.fail(400, str(error))
        connection = HTTPConnection("agent", 4096, timeout=60)
        started = False
        try:
            headers = {"Authorization": self.headers.get("Authorization", "")}
            if raw:
                headers["Content-Type"] = "application/json"
            connection.request(self.command, self.path, body=raw or None, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            started, total = True, 0
            while chunk := response.read1(65536):
                total += len(chunk)
                if total > MAX_CONTROL_RESPONSE:
                    break
                self.wfile.write(chunk)
        except (OSError, HTTPException):
            if not started:
                self.fail(502, "The coding engine is starting or unavailable.")
        finally:
            self.close_connection = True
            connection.close()


class EgressHandler(Handler):
    def refuse(self, host):
        return self.fail(403, f"Network access to {host or 'this destination'} is not allowed for this task.")

    def do_CONNECT(self):
        parsed = urlsplit("//" + self.path)
        try:
            host, port = parsed.hostname, parsed.port
        except ValueError:
            return self.refuse("")
        if parsed.username or parsed.password or port != 443 or not host_allowed(host, self.server.config["egress"]):
            return self.refuse(host)
        try:
            remote = connect_public(host, 443)
        except (ValueError, OSError):
            return self.refuse(host)
        with remote:
            self.send_response(200, "Connection established")
            self.end_headers()
            self.wfile.flush()
            self.tunnel(remote)

    def tunnel(self, remote):
        sockets, total, deadline = [self.connection, remote], 0, time.monotonic() + 1800
        while time.monotonic() < deadline and total < MAX_TUNNEL:
            readable, _, _ = select.select(sockets, [], [], 60)
            if not readable:
                break
            for source in readable:
                try:
                    data = source.recv(65536)
                    if not data:
                        return
                    total += len(data)
                    (remote if source is self.connection else self.connection).sendall(data)
                except OSError:
                    return
        self.close_connection = True

    def do_GET(self):
        self.plain()

    def do_HEAD(self):
        self.plain()

    def do_POST(self):
        self.plain()

    def plain(self):
        parsed = urlsplit(self.path)
        try:
            host, port = parsed.hostname, parsed.port
        except ValueError:
            return self.refuse("")
        if (parsed.scheme != "http" or parsed.username or parsed.password or port not in {None, 80}
                or not host_allowed(host, self.server.config["egress"])):
            return self.refuse(host)
        try:
            raw = self.body(False)
            remote = connect_public(host, 80)
        except (ValueError, OSError):
            return self.refuse(host)
        with remote:
            target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            lines = [f"{self.command} {target} HTTP/1.0", f"Host: {parsed.netloc}", "Connection: close"]
            skip = {"host", "connection", "proxy-connection", "proxy-authorization", "keep-alive", "upgrade", "te",
                    "trailer", "transfer-encoding"}
            lines += [f"{name}: {value}" for name, value in self.headers.items() if name.lower() not in skip]
            remote.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + raw)
            total = 0
            try:
                while total < MAX_TUNNEL and (data := remote.recv(65536)):
                    total += len(data)
                    self.wfile.write(data)
            except OSError:
                pass
        self.close_connection = True


def main():
    with open(sys.argv[1], encoding="utf-8") as source:
        config = json.load(source)
    config.setdefault("egress", ["*"])
    servers = [Server(("0.0.0.0", 8080), EgressHandler, config), Server(("0.0.0.0", 8082), ControlHandler, config)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    Server(("0.0.0.0", 8000), InferenceHandler, config).serve_forever()


if __name__ == "__main__":
    main()
