"""The mitmproxy addon. Every HTTP(S) request that reaches the gateway (because our DNS sent
it here) is answered from the archive, our search index, the control API, or — for hosts on
the passthrough list only — the real internet."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from mitmproxy import http

from . import config, pages
from .sources import SourceError, build_sources
from .store import Page, Store, extract_text

log = logging.getLogger("frozen")

JOIN_SH = Path(__file__).resolve().parent.parent / "join.sh"
SELFTEST_SH = JOIN_SH.with_name("selftest.sh")

# What non-allowlisted visitors may see on the control host.
PUBLIC_PATHS = {"/", "/healthz", "/ca.pem", "/ca.crt", "/join.sh", "/selftest.sh"}
ACME_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,256}")
_ALLOW_NETS = [ipaddress.ip_network(c, strict=False) for c in config.ALLOW]


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


class RateLimiter:
    """Per-IP token bucket."""

    def __init__(self, rate: float, burst: int):
        self.rate, self.burst = rate, burst
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, ip: str) -> bool:
        if self.rate <= 0:
            return True
        now = time.monotonic()
        if len(self.buckets) > 100_000:
            self.buckets.clear()
        tokens, last = self.buckets.get(ip, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1:
            self.buckets[ip] = (tokens, now)
            return False
        self.buckets[ip] = (tokens - 1, now)
        return True


def primary_domain() -> str:
    """The public HTTPS domain, once its real certificate is installed."""
    if config.SITE_DOMAINS and config.SITE_CERT.exists():
        return next((d for d in config.SITE_DOMAINS if not d.startswith("www.")), config.SITE_DOMAINS[0])
    return ""


def ip_allowed(ip: str) -> bool:
    if not _ALLOW_NETS:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or any(addr in n for n in _ALLOW_NETS)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _client_ip(flow: http.HTTPFlow) -> str:
    ip = flow.client_conn.peername[0] if flow.client_conn.peername else "?"
    return ip[7:] if ip.startswith("::ffff:") else ip


def _host(flow: http.HTTPFlow) -> str:
    h = (flow.request.host_header or flow.request.pretty_host or "").lower()
    if h.startswith("["):
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def canonical_url(flow: http.HTTPFlow) -> str:
    scheme = "https" if flow.client_conn.tls_established else "http"
    return f"{scheme}://{_host(flow)}{flow.request.path}"


def respond(flow, status: int, body: bytes, ctype: str = "text/html; charset=utf-8",
            extra: list[tuple[str, str]] | None = None) -> None:
    headers = [("Content-Type", ctype), ("Cache-Control", "no-store")] + (extra or [])
    flow.response = http.Response.make(status, body, [(k.encode(), v.encode()) for k, v in headers])


def respond_json(flow, obj, status: int = 200) -> None:
    respond(flow, status, json.dumps(obj, indent=1).encode(), "application/json")


class FrozenWeb:
    def __init__(self, store: Store | None = None, sources=None):
        self.store = store or Store(config.DATA_DIR / "frozen.sqlite")
        self.sources = sources if sources is not None else build_sources(config.SOURCES)
        self._inflight: dict[tuple[str, str], asyncio.Future] = {}
        self.limiter = RateLimiter(config.HTTP_RATE, config.HTTP_BURST)

    async def running(self):
        # Fetch Common Crawl's index files in the background so the first lookups are fast.
        for src in self.sources:
            if hasattr(src, "warm"):
                asyncio.get_running_loop().create_task(src.warm(config.DEFAULT_SNAPSHOT))

    # ---- archive resolution ----------------------------------------------
    async def resolve(self, url: str, snapshot: str) -> tuple[Page | None, str]:
        """Return (page, reason). Results — including clean misses — are pinned in the cache
        so every run against the same snapshot sees exactly the same web."""
        cached = self.store.get_page(snapshot, url)
        if cached:
            return (None, "cached miss") if cached.source == "miss" else (cached, "cache")
        key = (snapshot, url)
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await self._resolve_uncached(url, snapshot)
            fut.set_result(result)
            return result
        except Exception as e:  # never leave waiters hanging
            fut.set_result((None, f"error: {e}"))
            return None, f"error: {e}"
        finally:
            self._inflight.pop(key, None)

    async def _resolve_uncached(self, url: str, snapshot: str) -> tuple[Page | None, str]:
        errors = []
        for src in self.sources:
            try:
                page = await src.lookup(url, snapshot)
            except (SourceError, Exception) as e:
                errors.append(f"{src.name}: {e}")
                log.warning("source %s failed for %s: %s", src.name, url, e)
                continue
            if page is not None:
                page.url = url
                self.store.put_page(snapshot, page)
                return page, page.source
        if errors:
            return None, "; ".join(errors)  # transient: don't pin
        self.store.put_page(snapshot, Page(url, 404, [], b"", "miss", ""))
        return None, "no capture"

    # ---- mitmproxy hook ----------------------------------------------------
    async def request(self, flow: http.HTTPFlow) -> None:
        ip = _client_ip(flow)
        host = _host(flow)
        snapshot = self.store.client_snapshot(ip) or config.DEFAULT_SNAPSHOT
        path = urlsplit(flow.request.path).path

        # Let's Encrypt HTTP-01 challenge (must work for the whole internet, over plain HTTP).
        if path.startswith("/.well-known/acme-challenge/"):
            return self.acme(flow, path)

        # Public website domain over plain HTTP -> HTTPS once a real cert is installed.
        if (host in config.SITE_DOMAINS and not flow.client_conn.tls_established
                and config.SITE_CERT.exists()):
            respond(flow, 301, b"", "text/plain", [("Location", f"https://{host}{flow.request.path}")])
            return

        if path != "/healthz" and not _is_loopback(ip) and not self.limiter.allow(ip):
            respond(flow, 429, pages.error_page("Slow down",
                    "Too many requests from your IP. The limit refills every second."),
                    extra=[("Retry-After", "5")])
            return self._log(flow, ip, snapshot, "ratelimited")

        if not ip_allowed(ip):
            if (host in config.CONTROL_HOSTS or _is_ip(host)) and path in PUBLIC_PATHS:
                await self.control(flow, ip, snapshot)
            else:
                respond(flow, 403, pages.error_page("Not allowed",
                        f"{ip} is not on this frozen web's allowlist."))
            return self._log(flow, ip, snapshot, "denied")

        if host in config.CONTROL_HOSTS or _is_ip(host) or host == config.PUBLIC_IP:
            await self.control(flow, ip, snapshot)
            return self._log(flow, ip, snapshot, "control")

        if any(host == p or host.endswith("." + p) for p in config.PASSTHROUGH):
            # Let it go to the real host. Everything else stays sealed.
            flow.request.scheme = "https" if flow.client_conn.tls_established else "http"
            flow.request.host = host
            flow.request.port = 443 if flow.request.scheme == "https" else 80
            flow.metadata["frozen_source"] = "passthrough"
            return

        qs = parse_qs(urlsplit(flow.request.path).query)
        bare_path = urlsplit(flow.request.path).path
        if host in config.SEARCH_HOSTS and bare_path in ("/search", "/html", "/html/", "/"):
            q = (qs.get("q") or [""])[0]
            raw_start = (qs.get("start") or qs.get("first") or ["0"])[0]
            start = min(int(raw_start), 500) if raw_start.isdigit() else 0
            results = self.store.search(q, snapshot, limit=10, offset=start) if q else []
            respond(flow, 200, pages.search_page(q, results, snapshot, bare_path, start // 10))
            return self._log(flow, ip, snapshot, "search")

        if flow.request.method not in ("GET", "HEAD"):
            respond(flow, 405, pages.error_page("Read-only web",
                    "The frozen web only serves archived pages; forms and writes are disabled."))
            return self._log(flow, ip, snapshot, "blocked")

        url = canonical_url(flow)
        page, reason = await self.resolve(url, snapshot)
        if page is None:
            status = 504 if reason.startswith(("error", "commoncrawl", "wayback")) else 404
            respond(flow, status, pages.miss_page(url, snapshot, reason if status == 504 else ""),
                    extra=[("X-Frozen-Source", "miss"), ("X-Frozen-Snapshot", snapshot)])
            return self._log(flow, ip, snapshot, "miss" if status == 404 else "error")

        hdrs = page.clean_headers() + [
            ("X-Frozen-Source", page.source), ("X-Frozen-Captured", page.captured),
            ("X-Frozen-Snapshot", snapshot)]
        body = b"" if flow.request.method == "HEAD" else page.body
        flow.response = http.Response.make(page.status or 200, body,
                                           [(k.encode(), v.encode("utf-8", "replace")) for k, v in hdrs])
        self._log(flow, ip, snapshot, page.source)

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("frozen_source") == "passthrough":
            ip = _client_ip(flow)
            self._log(flow, ip, self.store.client_snapshot(ip) or config.DEFAULT_SNAPSHOT, "passthrough")

    def _log(self, flow, ip, snapshot, source):
        status = flow.response.status_code if flow.response else 0
        try:
            self.store.log(ip, snapshot, flow.request.method, canonical_url(flow), status, source)
        except Exception as e:
            log.warning("log failed: %s", e)

    def acme(self, flow, path: str) -> None:
        token = path.rsplit("/", 1)[-1]
        f = config.ACME_DIR / token
        if not ACME_TOKEN.fullmatch(token) or not f.is_file():
            return respond(flow, 404, b"not found", "text/plain")
        respond(flow, 200, f.read_bytes(), "text/plain")

    # ---- control plane -----------------------------------------------------
    def _authed(self, flow) -> bool:
        auth = flow.request.headers.get("authorization", "")
        return bool(config.ADMIN_TOKEN) and auth == f"Bearer {config.ADMIN_TOKEN}"

    async def control(self, flow: http.HTTPFlow, ip: str, snapshot: str) -> None:
        parts = urlsplit(flow.request.path)
        path, qs = parts.path, parse_qs(parts.query)
        arg = lambda k, d="": (qs.get(k) or [d])[0]

        if path == "/":
            return respond(flow, 200, pages.status_page(ip, snapshot, self.store.stats(), config.PUBLIC_IP,
                                                        ip_allowed(ip), primary_domain(),
                                                        open_access=not config.ALLOW))
        if path in ("/ca.pem", "/ca.crt"):
            if not config.CA_CERT_PATH.exists():
                return respond(flow, 503, b"CA not generated yet", "text/plain")
            return respond(flow, 200, config.CA_CERT_PATH.read_bytes(), "application/x-pem-file",
                           [("Content-Disposition", "attachment; filename=frozen-web-ca.pem")])
        if path == "/join.sh":
            script = (JOIN_SH.read_text().replace("__FROZEN_IP__", config.PUBLIC_IP)
                      .replace("__FROZEN_DOMAIN__", primary_domain()))
            return respond(flow, 200, script.encode(), "text/x-shellscript")
        if path == "/selftest.sh":
            base = primary_domain() or config.PUBLIC_IP
            script = SELFTEST_SH.read_text().replace("__FROZEN_DOMAIN__", base)
            if not primary_domain():
                script = script.replace(f"https://{base}", f"http://{base}")
            return respond(flow, 200, script.encode(), "text/x-shellscript")
        if path == "/api/whoami":
            return respond_json(flow, {"ip": ip, "snapshot": snapshot})
        if path == "/api/search":
            q = arg("q")
            snap = self._snap_arg(flow, qs, snapshot)
            n = min(int(arg("n", "10")) if arg("n", "10").isdigit() else 10, 50)
            return respond_json(flow, {"q": q, "snapshot": snap,
                                       "results": self.store.search(q, snap, limit=n)})
        if path == "/api/fetch":
            url = arg("url")
            if not url.startswith(("http://", "https://")):
                return respond_json(flow, {"error": "url must be absolute http(s)"}, 400)
            snap = self._snap_arg(flow, qs, snapshot)
            page, reason = await self.resolve(url, snap)
            if page is None:
                return respond_json(flow, {"url": url, "snapshot": snap, "found": False,
                                           "reason": reason}, 404)
            if arg("format") == "raw":
                flow.response = http.Response.make(page.status, page.body,
                    [(k.encode(), v.encode("utf-8", "replace")) for k, v in page.clean_headers()])
                return
            out = {"url": url, "snapshot": snap, "found": True, "status": page.status,
                   "captured": page.captured, "source": page.source,
                   "content_type": page.content_type(), "headers": page.clean_headers()}
            if "html" in page.content_type().lower():
                out["title"], out["text"] = extract_text(page.body)
            elif page.content_type().startswith(("text/", "application/json")):
                out["text"] = page.body.decode("utf-8", "replace")[:500_000]
            return respond_json(flow, out)
        if path == "/api/log":
            who = arg("client") if self._authed(flow) else ip
            if self._authed(flow) and arg("client") == "*":
                who = None
            lim = min(int(arg("limit", "200")) if arg("limit", "200").isdigit() else 200, 5000)
            return respond_json(flow, self.store.recent_log(who, lim))
        if path == "/api/snapshot" and flow.request.method == "POST":
            # Self-service, no human or token needed: a client may move ITSELF to an earlier
            # date (never later than the server's default, so nothing newer can leak in).
            try:
                body = json.loads(flow.request.get_text() or "{}")
            except json.JSONDecodeError:
                return respond_json(flow, {"error": "bad json"}, 400)
            snap = str(body.get("snapshot", ""))
            if snap in ("", "default"):
                self.store.set_client(ip, None)
                return respond_json(flow, {"ok": True, "ip": ip, "snapshot": config.DEFAULT_SNAPSHOT})
            if not config.valid_snapshot(snap) or snap > config.DEFAULT_SNAPSHOT:
                return respond_json(flow, {"error": f"snapshot must be YYYYMMDD and no later than "
                                                    f"{config.DEFAULT_SNAPSHOT}"}, 400)
            self.store.set_client(ip, snap, str(body.get("label", ""))[:100])
            return respond_json(flow, {"ok": True, "ip": ip, "snapshot": snap})
        if path == "/admin/clients" and flow.request.method == "GET":
            if not self._authed(flow):
                return respond_json(flow, {"error": "unauthorized"}, 401)
            return respond_json(flow, self.store.clients())
        if path == "/admin/client" and flow.request.method in ("POST", "PUT", "DELETE"):
            if not self._authed(flow):
                return respond_json(flow, {"error": "unauthorized"}, 401)
            try:
                body = json.loads(flow.request.get_text() or "{}")
            except json.JSONDecodeError:
                return respond_json(flow, {"error": "bad json"}, 400)
            cip = body.get("ip", "")
            if not _is_ip(cip):
                return respond_json(flow, {"error": "ip required"}, 400)
            if flow.request.method == "DELETE":
                self.store.set_client(cip, None)
                return respond_json(flow, {"ok": True, "ip": cip, "snapshot": config.DEFAULT_SNAPSHOT})
            snap = str(body.get("snapshot", ""))
            if not config.valid_snapshot(snap):
                return respond_json(flow, {"error": "snapshot must be YYYYMMDD"}, 400)
            self.store.set_client(cip, snap, str(body.get("label", "")))
            return respond_json(flow, {"ok": True, "ip": cip, "snapshot": snap})
        if path == "/healthz":
            return respond(flow, 200, b"ok", "text/plain")
        respond_json(flow, {"error": "not found"}, 404)

    def _snap_arg(self, flow, qs, default: str) -> str:
        """Agents can't pick their own (possibly newer) snapshot; only the admin can override."""
        s = (qs.get("snapshot") or [""])[0]
        if s and self._authed(flow) and config.valid_snapshot(s):
            return s
        return default
