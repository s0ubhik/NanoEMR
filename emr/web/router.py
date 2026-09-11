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
    # The whole tail, slashes included — for a route that must answer a family
    # of paths rather than one exact URL (the NHCX callback door).
    "path": (r".+", str),
}

# `<name>`, `<int:name>`, `<path:name>` … — the kinds come from _CONVERTERS so
# adding one there is enough to make it usable in a route.
_PARAM = re.compile(r"<(?:(" + "|".join(_CONVERTERS) + r"):)?"
                    r"([a-zA-Z_][a-zA-Z0-9_]*)>")

# ---------------------------------------------------------------------------
# mount prefix
# ---------------------------------------------------------------------------
# The app speaks canonical root-relative paths internally — routes, `NAV`, every
# `href` and every redirect. A deployment behind a reverse proxy that serves it
# from a sub-path sets the prefix once here, and it is applied at the edge only:
# taken off the way in (`_routable`), pasted back on the way out (`_mounted`).
# Nothing in the route or UI layer knows about it, which is why mounting cannot
# leave a link behind. Coming in the prefix is optional — a proxy that strips it
# before forwarding is just as well served; see `_routable`.
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


def _routable(path: str) -> list[str]:
    """The paths the routing table should be tried against, best guess first.

    A reverse proxy mounts the app one of two ways, and both are common:

    * it forwards the request path whole (nginx ``proxy_pass http://host:port;``),
      so the app sees ``/nanoemr/static/logo.png`` and has to take the prefix
      off itself;
    * it strips the prefix before forwarding (``proxy_pass http://host:port/;``,
      the trailing slash), so the app sees ``/static/logo.png`` already routable.

    The app cannot tell the two apart from the request, so it answers both: the
    prefixed reading is tried first, the bare path second. Outgoing links are
    unaffected — they always carry the prefix — so a browser keeps asking for
    ``/nanoemr/…`` either way. This also keeps a caller that reaches the port
    directly, bypassing the proxy (hcxkit posting to ``/callback``), working
    against a mounted app.
    """
    if not _BASE:
        return [path]
    if path == _BASE:
        return ["/"]
    if path.startswith(_BASE + "/"):
        # Both readings: the prefix may be the mount point, or — behind a
        # stripping proxy — a route that genuinely starts with those segments.
        return [path[len(_BASE):], path]
    return [path]


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


_DISPOSITION = re.compile(r'(name|filename)="((?:[^"\\]|\\.)*)"')


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, list[str]],
                                                             dict[str, list[dict]]]:
    """Split a ``multipart/form-data`` body into text fields and file parts.

    Returns ``(form, files)`` in the same ``{name: [values]}`` shape the
    urlencoded path produces; each file is ``{"filename", "content_type",
    "data"}``. The stdlib lost its multipart parser with ``cgi``, hence this
    deliberately small one: it handles what browsers actually send.
    """
    form: dict[str, list[str]] = {}
    files: dict[str, list[dict]] = {}
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not match:
        return form, files
    delimiter = b"--" + match.group(1).strip().encode("utf-8")
    for part in body.split(delimiter)[1:]:
        if part.startswith(b"--"):  # the closing delimiter
            break
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        head, separator, data = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        name = filename = None
        part_type = "application/octet-stream"
        for line in head.decode("utf-8", "replace").split("\r\n"):
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if key == "content-disposition":
                for attr, raw in _DISPOSITION.findall(value):
                    decoded = raw.replace('\\"', '"').replace("\\\\", "\\")
                    if attr == "name":
                        name = decoded
                    else:
                        filename = decoded
            elif key == "content-type":
                part_type = value.strip().split(";")[0].strip() or part_type
        if name is None:
            continue
        if filename is None:
            form.setdefault(name, []).append(data.decode("utf-8", "replace"))
        elif filename or data:  # skip the empty part an unused file input posts
            files.setdefault(name, []).append({
                "filename": filename, "content_type": part_type, "data": data})
    return form, files


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
                 headers: dict[str, str] | None = None,
                 files: dict[str, list[dict]] | None = None):
        self.method = method
        self.path = path
        self._query = query
        self._form = form
        self.cookies = cookies
        self.params = params
        self.raw_body = body
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._files = files or {}

    def field_names(self, prefix: str = "") -> list[str]:
        """Submitted field names, optionally only those carrying a prefix.

        For repeating groups whose names are only known at submit time — a
        questionnaire's answers are keyed by the payer's own link ids.
        """
        return [name for name in self._form if name.startswith(prefix)]

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

    def files_map(self) -> dict[str, list[dict]]:
        """Every uploaded part, by field name.

        For forms whose file fields are only known at submit time — a
        questionnaire's attachment questions are keyed by the payer's link ids.
        """
        return dict(self._files)

    def files_all(self, name: str) -> list[dict]:
        """Every uploaded file posted under this field name (multipart only)."""
        return self._files.get(name, [])

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
        for match in _PARAM.finditer(pattern):
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
                 headers: dict[str, str] | None = None,
                 files: dict[str, list[dict]] | None = None) -> Response:
        return _mounted(self._dispatch(method, path, query, form, cookies, body,
                                       headers, files))

    def _dispatch(self, method: str, path: str, query: dict, form: dict,
                  cookies: SimpleCookie, body: bytes,
                  headers: dict[str, str] | None = None,
                  files: dict[str, list[dict]] | None = None) -> Response:
        allowed = False
        for candidate in _routable(path):
            for route_method, regex, converters, fn in self.routes:
                match = regex.match(candidate)
                if not match:
                    continue
                if route_method != method:
                    allowed = True
                    continue
                params = {name: conv(match.group(name)) for name, conv in converters}
                request = Request(method, candidate, query, form, cookies, params,
                                  body, headers, files)
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
            # Either spelling of an asset path is noise: the prefix is optional
            # on the way in, so both reach us depending on the proxy.
            if not any(candidate.startswith("/static")
                       for candidate in _routable(urlparse(self.path).path)):
                print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

        def _run(self, method: str, body_out: bool = True) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            body = b""
            form: dict[str, list[str]] = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                body = self.rfile.read(length)
            files: dict[str, list[dict]] = {}
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype == "application/x-www-form-urlencoded":
                form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            elif ctype == "multipart/form-data":
                form, files = parse_multipart(
                    body, self.headers.get("Content-Type") or "")
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
                                        cookies, body, dict(self.headers.items()),
                                        files)
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
