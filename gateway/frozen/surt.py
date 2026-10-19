"""SURT keys the way Common Crawl's index sorts them (a dependency-free port of the
behaviour of internetarchive/surt's default canonicalizer, for the common cases)."""
from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit

_WWW = re.compile(r"^www\d*\.")
_DEFAULT_PORT = {"http": "80", "https": "443"}


def _unescape(s: str) -> str:
    for _ in range(10):
        u = unquote(s, errors="surrogateescape")
        if u == s:
            break
        s = u
    return s


def _escape(s: str) -> str:
    return quote(s, safe="!$&'()*+,;=:@/~-._", errors="surrogateescape")


def _norm_path(path: str) -> str:
    path = re.sub(r"/{2,}", "/", path or "/")
    out: list[str] = []
    for seg in path.split("/")[1:]:
        if seg == ".":
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    path = "/" + "/".join(out)
    return path


def surt(url: str) -> str:
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower().strip(".")
    host = _WWW.sub("", host)
    port = parts.port
    hostkey = ",".join(reversed(host.split("."))) if host else ""
    if port and str(port) != _DEFAULT_PORT.get(scheme):
        hostkey += f":{port}"

    path = _escape(_norm_path(_unescape(parts.path))).lower()
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = ""
    if parts.query:
        args = [a for a in _escape(_unescape(parts.query)).lower().split("&") if a]
        query = "&".join(sorted(args))
    return f"{hostkey}){path}" + (f"?{query}" if query else "")
