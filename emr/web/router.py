"""A very small routing + request/response layer over http.server.

Named `router` rather than `http` so it does not shadow the stdlib package.
"""

from __future__ import annotations

import json
import re
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse, quote, unquote

Handler = Callable[["Request"], "Response"]

_CONVERTERS = {
    "int": (r"[0-9]+", int),
    "str": (r"[^/]+", str),
}

# ---------------------------------------------------------------------------
# mount prefix
# ---------------------------------------------------------------------------
# The app speaks canonical root-relative paths internally — routes, `NAV`, every
# `href` and every redirect. A deployment behind a reverse proxy that serves it
# from a sub-path sets the prefix once here, and it is applied at the edge only:
# stripped off the way in (`dispatch`), pasted back on the way out (`_mounted`).
# Nothing in the route or UI layer knows about it, which is why mounting cannot
# leave a link behind.
_BASE = ""

# Segments are deliberately narrow: a prefix comes from an operator's flag, and
# anything needing escaping is a mistake worth refusing at startup.
_BASE_PATTERN = re.compile(r"^(/[A-Za-z0-9._~-]+)+$")

# The three attributes that carry an app path in this codebase (48 `action`,
# 39 `href`, 1 `src`). `(?!/)` leaves protocol-relative URLs alone; anything
# absolute (the CDN) never starts with a slash.
_LINK = re.compile(r'((?:href|action|src)=")/(?!/)')

_COOKIE_PATH = re.compile(r"(;\s*Path=)/")


def set_base(prefix: str) -> str:
    """Mount every route under a URL prefix, and return the prefix as stored.

    Accepts ``/emr``, ``emr``, ``/emr/`` or a full base URI
    (``https://host/emr``, of which only the path is used). Empty, ``/`` or
    ``None`` mean "serve from the root", which is the default. Raises
    ``ValueError`` on a prefix that is not a plain path.
    """
    global _BASE
    _BASE = _normalise_base(prefix)
    return _BASE


def base() -> str:
    """The current mount prefix: ``""`` or something like ``/emr``."""
    return _BASE


def url(path: str) -> str:
    """A canonical app path as the outside world must ask for it."""
    if not _BASE or not path.startswith("/"):
        return path
    return _BASE + ("" if path == "/" else path)


def _normalise_base(prefix: str | None) -> str:
    raw = (prefix or "").strip()
    if "://" in raw:
        raw = urlparse(raw).path
    raw = raw.rstrip("/")
    if not raw:
        return ""
    if not raw.startswith("/"):
        raw = "/" + raw
    if not _BASE_PATTERN.match(raw) or any(
            segment in (".", "..") for segment in raw.split("/")):
        raise ValueError(
            f"base URI {prefix!r} is not a usable path prefix — expected "
            "something like /emr (letters, digits, '.', '_', '~' and '-')")
    return raw


def _strip_base(path: str) -> str | None:
    """The path as the routing table knows it, or ``None`` if it is off-mount."""
    if not _BASE:
        return path
    if path == _BASE:
        return "/"
    if path.startswith(_BASE + "/"):
        return path[len(_BASE):]
    return None


def _mounted(response: Response) -> Response:
    """Rewrite the one response so every path it hands out carries the prefix."""
    if not _BASE:
        return response
    is_html = False
    headers = []
    for key, value in response.headers:
        name = key.lower()
        if name == "location" and value.startswith("/") and not value.startswith("//"):
            value = _BASE + value
        elif name == "set-cookie":
            value = _COOKIE_PATH.sub(lambda m: m.group(1) + _BASE + "/", value)
        elif name == "content-type" and value.startswith("text/html"):
            is_html = True
        headers.append((key, value))
    response.headers = headers
    if is_html and response.body:
        body = _LINK.sub(lambda m: m.group(1) + _BASE + "/",
                         response.body.decode("utf-8"))
        response.body = body.encode("utf-8")
    return response


class Response:
    def __init__(self, body: bytes | str = b"", status: int = 200,
                 content_type: str = "text/html; charset=utf-8",
                 headers: list[tuple[str, str]] | None = None):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.headers = headers or []
        if content_type:
            self.headers.append(("Content-Type", content_type))


def html(body: str, status: int = 200) -> Response:
    """An HTML response."""
    return Response(body, status)


def redirect(location: str, flash: str | None = None, kind: str = "success") -> Response:
    """A 303 redirect, optionally carrying a one-shot flash message."""
    headers = [("Location", location)]
    if flash:
        headers.append((
            "Set-Cookie",
            f"flash={quote(kind + '|' + flash)}; Path=/; SameSite=Lax",
        ))
    return Response(b"", 303, content_type="", headers=headers)


def json_response(payload: Any, status: int = 200, download: str | None = None) -> Response:
    """A JSON response, optionally offered as a download."""
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    headers = []
    if download:
        headers.append(("Content-Disposition", f'attachment; filename="{download}"'))
    return Response(body, status, "application/json; charset=utf-8", headers)



class Request:
    def __init__(self, method: str, path: str, query: dict[str, list[str]],
                 form: dict[str, list[str]], cookies: SimpleCookie,
                 params: dict[str, Any], body: bytes,
                 headers: dict[str, str] | None = None):
        self.method = method
        self.path = path
        self._query = query
        self._form = form
        self.cookies = cookies
        self.params = params
        self.raw_body = body
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}

    def header(self, name: str, default: str = "") -> str:
        """One request header, matched case-insensitively."""
        return self._headers.get(name.lower(), default)

    # ---- accessors -------------------------------------------------------
    def q(self, name: str, default: str = "") -> str:
        return self._query.get(name, [default])[0]

    def q_all(self, name: str) -> list[str]:
        return self._query.get(name, [])

    def f(self, name: str, default: str = "") -> str:
        return self._form.get(name, [default])[0].strip()

    def f_all(self, name: str) -> list[str]:
        return [v.strip() for v in self._form.get(name, [])]

    def f_int(self, name: str, default: int | None = None) -> int | None:
        raw = self.f(name)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def f_float(self, name: str, default: float = 0.0) -> float:
        raw = self.f(name)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def f_or_none(self, name: str) -> str | None:
        return self.f(name) or None

    def flash(self) -> tuple[str, str] | None:
        cookie = self.cookies.get("flash")
        if not cookie:
            return None
        try:
            kind, _, message = unquote(cookie.value).partition("|")
        except Exception:
            return None
        return kind, message


class App:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern, list[tuple[str, Callable]], Handler]] = []
        self.error_page: Callable[[int, str], str] | None = None

    def route(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        def decorator(fn: Handler) -> Handler:
            self.add(method, pattern, fn)
            return fn
        return decorator

    def get(self, pattern: str):
        return self.route("GET", pattern)

    def post(self, pattern: str):
        return self.route("POST", pattern)

    def add(self, method: str, pattern: str, fn: Handler) -> None:
        converters: list[tuple[str, Callable]] = []
        regex = "^"
        index = 0
        for match in re.finditer(r"<(?:(int|str):)?([a-zA-Z_][a-zA-Z0-9_]*)>", pattern):
            regex += re.escape(pattern[index:match.start()])
            kind = match.group(1) or "str"
            name = match.group(2)
            rx, conv = _CONVERTERS[kind]
            regex += f"(?P<{name}>{rx})"
            converters.append((name, conv))
            index = match.end()
        regex += re.escape(pattern[index:]) + "$"
        self.routes.append((method, re.compile(regex), converters, fn))

    def dispatch(self, method: str, path: str, query: dict, form: dict,
                 cookies: SimpleCookie, body: bytes,
                 headers: dict[str, str] | None = None) -> Response:
        return _mounted(self._dispatch(method, path, query, form, cookies, body,
                                       headers))

    def _dispatch(self, method: str, path: str, query: dict, form: dict,
                  cookies: SimpleCookie, body: bytes,
                  headers: dict[str, str] | None = None) -> Response:
        routed = _strip_base(path)
        if routed is None:  # outside the mount point entirely
            return self._error(404, "Not found")
        path = routed
        allowed = False
        for route_method, regex, converters, fn in self.routes:
            match = regex.match(path)
            if not match:
                continue
            if route_method != method:
                allowed = True
                continue
            params = {name: conv(match.group(name)) for name, conv in converters}
            request = Request(method, path, query, form, cookies, params, body,
                              headers)
            return fn(request)
        if allowed:
            return self._error(405, "Method not allowed")
        return self._error(404, "Not found")

    def _error(self, status: int, message: str) -> Response:
        if self.error_page:
            return Response(self.error_page(status, message), status)
        return Response(f"{status} {message}", status, "text/plain; charset=utf-8")


def make_handler(app: App):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "NanoEMR"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
            if not self.path.startswith(_BASE + "/static"):
                print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

        def _run(self, method: str, body_out: bool = True) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            body = b""
            form: dict[str, list[str]] = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                body = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype == "application/x-www-form-urlencoded":
                form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            elif ctype == "application/json" and body:
                try:
                    payload = json.loads(body.decode("utf-8"))
                    if isinstance(payload, dict):
                        form = {k: [str(v)] for k, v in payload.items()}
                except ValueError:
                    pass
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie") or "")

            try:
                response = app.dispatch(method, unquote(parsed.path), query, form,
                                        cookies, body, dict(self.headers.items()))
            except Exception:  # pragma: no cover - surfaced in the browser
                traceback.print_exc()
                response = app._error(500, traceback.format_exc())

            self.send_response(response.status)
            for key, value in response.headers:
                self.send_header(key, value)
            if response.status != 304:
                self.send_header("Content-Length", str(len(response.body)))
            if not any(k.lower() == "set-cookie" for k, _ in response.headers):
                # Clear a consumed flash message.
                if cookies.get("flash"):
                    self.send_header("Set-Cookie",
                                     f"flash=; Path={_BASE}/; Max-Age=0")
            self.end_headers()
            if body_out and response.body:
                self.wfile.write(response.body)

        def do_GET(self) -> None:
            self._run("GET")

        def do_HEAD(self) -> None:
            # Same headers as the GET, no body. Browsers, proxies and `curl -I`
            # all use it; without this they get a 501.
            self._run("GET", body_out=False)

        def do_POST(self) -> None:
            self._run("POST")

    return _Handler


def serve(app: App, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the application on a threading HTTP server until interrupted."""
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"\n  NanoEMR running at http://{host}:{port}{_BASE}/\n  Ctrl-C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        server.server_close()
