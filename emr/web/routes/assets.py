"""Static assets.

The app ships a handful of small files (the logo and the favicon) and nothing
else, so this serves an explicit allow-list rather than a directory. That rules
out path traversal by construction: a name that is not a key here never reaches
the filesystem.

Assets are read once and held in memory — they are a few tens of kilobytes and
never change at runtime — and served with a long cache lifetime plus an ETag so
a browser fetches each one once.
"""

from __future__ import annotations

import hashlib
import os

from ..router import Request, Response

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "static")

# public name -> (file on disk, content type)
ASSETS: dict[str, tuple[str, str]] = {
    "logo.png": ("logo.png", "image/png"),
    "favicon.png": ("favicon.png", "image/png"),
}

_cache: dict[str, tuple[bytes, str, str]] = {}


def load(name: str) -> tuple[bytes, str, str] | None:
    """Return ``(body, content_type, etag)`` for an allow-listed asset."""
    if name not in ASSETS:
        return None
    if name not in _cache:
        filename, content_type = ASSETS[name]
        path = os.path.join(STATIC_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            body = fh.read()
        etag = '"%s"' % hashlib.sha256(body).hexdigest()[:16]
        _cache[name] = (body, content_type, etag)
    return _cache[name]


def register(app) -> None:
    app.add("GET", "/static/<str:name>", serve)


def serve(request: Request):
    asset = load(request.params["name"])
    if asset is None:
        return Response(b"", 404, content_type="")
    body, content_type, etag = asset
    if request.header("If-None-Match") == etag:
        return Response(b"", 304, content_type="",
                        headers=[("ETag", etag)])
    return Response(body, 200, content_type,
                    headers=[("Cache-Control", "public, max-age=604800"),
                             ("ETag", etag)])
