"""A small JSON-over-HTTPS client built on the standard library.

BananaVibe holds a forge token and model keys, so the client is deliberately
strict: it never follows redirects (credentials must not travel to another
origin), ignores proxy and .netrc settings from the environment, bounds every
response body, and never includes response bodies or headers in exceptions.
"""

from dataclasses import dataclass
import json
import time
import urllib.error
import urllib.request

from . import USER_AGENT


class HTTPError(RuntimeError):
    """A non-2xx response. `body` is kept for callers but never printed."""

    def __init__(self, status, method, url, body=b"", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
        host = url.split("/")[2] if "://" in url else "?"
        super().__init__(f"{method} request to {host} returned HTTP {status}.")


class Unreachable(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        return json.loads(self.body) if self.body else None


def request(method, url, *, headers=None, body=None, timeout=45, limit=8 * 1024 * 1024,
            retries=0, sleep=time.sleep):
    """Send one request; `body` may be bytes or a JSON-serializable value.

    Only requests the caller marks as safe to repeat are retried, on transport
    errors and on 429/502/503/504 (honouring a short Retry-After).
    """
    data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
    all_headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    if data is not None and not isinstance(body, bytes):
        all_headers.setdefault("Content-Type", "application/json")
    for attempt in range(retries + 1):
        prepared = urllib.request.Request(url, data=data, method=method, headers=all_headers)
        try:
            with _OPENER.open(prepared, timeout=timeout) as response:
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise Unreachable(f"The response from {url.split('/')[2]} exceeded {limit} bytes.")
                return Response(response.status, dict(response.headers), raw)
        except urllib.error.HTTPError as error:
            raw = error.read(65536) if error.fp else b""
            status = error.code
            retry_after = error.headers.get("Retry-After", "") if error.headers else ""
            if attempt < retries and status in {429, 502, 503, 504}:
                sleep(min(int(retry_after), 60) if retry_after.isdigit() else 2 ** attempt)
                continue
            raise HTTPError(status, method, url, raw, dict(error.headers or {})) from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            if attempt < retries:
                sleep(2 ** attempt)
                continue
            host = url.split("/")[2] if "://" in url else url
            raise Unreachable(f"{host} could not be reached ({type(error).__name__}).") from None
    raise AssertionError("unreachable")
