"""Archive backends. Each returns the newest capture of a URL taken on or before a cutoff
date, so nothing newer than the snapshot can ever leak through.

lookup() result:
  Page        -> found
  None        -> the source answered and has no capture (safe to cache as a miss)
  raises      -> transient error (don't cache)
"""
from __future__ import annotations

import asyncio
import bisect
import gzip
import io
import json
import re
import time
from pathlib import Path

from urllib.parse import unquote, urlsplit

import httpx
from warcio.archiveiterator import ArchiveIterator

from . import config
from .store import Page
from .surt import surt

UA = "frozen-web/1.0 (agent sandbox archive replay)"


class SourceError(Exception):
    pass


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=config.FETCH_TIMEOUT, headers={"User-Agent": UA},
                             follow_redirects=False)


# ---------------------------------------------------------------------------
# Common Crawl: read the ZipNum CDX index straight from data.commoncrawl.org.
#
# index.commoncrawl.org (the CDX server) is frequently overloaded and answers 503/504, so we
# don't depend on it. Instead, per crawl we download the small top-level cluster.idx (~100 MB)
# once, binary-search it for the URL's SURT key, range-read the one ~200 KB gzipped block of
# the CDX shard that holds it, then range-read the WARC record. All three are plain static
# files on Common Crawl's CDN.
# ---------------------------------------------------------------------------
class ClusterIndex:
    """In-memory view of one crawl's cluster.idx: sorted block keys -> (shard, offset, length)."""

    def __init__(self, path: Path):
        self.keys: list[str] = []
        self.blocks: list[tuple[str, int, int]] = []
        with open(path, "rb") as f:
            for line in f:
                parts = line.rstrip(b"\n").split(b"\t")
                if len(parts) < 4:
                    continue
                self.keys.append(parts[0].decode("utf-8", "replace"))
                self.blocks.append((parts[1].decode(), int(parts[2]), int(parts[3])))

    def start(self, key: str) -> int:
        """Index of the block that can hold the first record whose SURT is >= key."""
        return max(0, bisect.bisect_left(self.keys, key) - 1)


class CommonCrawl:
    name = "commoncrawl"
    COLLINFO = "https://index.commoncrawl.org/collinfo.json"
    DATA = "https://data.commoncrawl.org/"

    def __init__(self, crawls_per_lookup: int = 2, cache_dir: Path | None = None):
        self.crawls_per_lookup = crawls_per_lookup
        self.cache_dir = cache_dir or (config.DATA_DIR / "cc-index")
        self._collinfo: list[dict] | None = None
        self._collinfo_at = 0.0
        self._clusters: dict[str, ClusterIndex] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sem = asyncio.Semaphore(16)

    # ---- which crawls ------------------------------------------------------
    async def crawls_before(self, client: httpx.AsyncClient, snapshot: str) -> list[dict]:
        await self._load_collinfo(client)
        return self.select_crawls(self._collinfo, snapshot, self.crawls_per_lookup)

    async def _load_collinfo(self, client: httpx.AsyncClient) -> None:
        if self._collinfo is not None and time.time() - self._collinfo_at < 86400:
            return
        disk = self.cache_dir / "collinfo.json"
        try:
            r = await client.get(self.COLLINFO)
            if r.status_code != 200:
                raise SourceError(f"collinfo {r.status_code}")
            self._collinfo = r.json()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            disk.write_text(json.dumps(self._collinfo))
        except Exception as e:
            if not disk.exists():
                raise SourceError(f"collinfo unavailable: {e}")
            self._collinfo = json.loads(disk.read_text())  # stale copy is fine: old crawls never change
        self._collinfo_at = time.time()

    @staticmethod
    def select_crawls(collinfo: list[dict], snapshot: str, n: int) -> list[dict]:
        cutoff = f"{snapshot[:4]}-{snapshot[4:6]}-{snapshot[6:8]}T23:59:59"
        ok = [c for c in collinfo if c.get("to", "9999") <= cutoff]
        ok.sort(key=lambda c: c["to"], reverse=True)
        return ok[:n]

    # ---- cluster.idx -------------------------------------------------------
    async def cluster(self, client: httpx.AsyncClient, crawl_id: str) -> ClusterIndex:
        if crawl_id in self._clusters:
            return self._clusters[crawl_id]
        lock = self._locks.setdefault(crawl_id, asyncio.Lock())
        async with lock:
            if crawl_id in self._clusters:
                return self._clusters[crawl_id]
            path = self.cache_dir / crawl_id / "cluster.idx"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                url = f"{self.DATA}cc-index/collections/{crawl_id}/indexes/cluster.idx"
                async with client.stream("GET", url, timeout=600) as r:
                    if r.status_code != 200:
                        raise SourceError(f"cluster.idx {r.status_code}")
                    with open(tmp, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 20):
                            f.write(chunk)
                tmp.rename(path)
            idx = await asyncio.to_thread(ClusterIndex, path)
            self._clusters[crawl_id] = idx
            return idx

    async def _block(self, client, crawl_id: str, shard: str, off: int, length: int) -> list[str]:
        url = f"{self.DATA}cc-index/collections/{crawl_id}/indexes/{shard}"
        async with self._sem:
            r = await client.get(url, headers={"Range": f"bytes={off}-{off + length - 1}"})
        if r.status_code not in (200, 206):
            raise SourceError(f"cdx block {r.status_code}")
        return gzip.decompress(r.content).decode("utf-8", "replace").splitlines()

    async def records(self, client, crawl_id: str, key: str, prefix: bool = False,
                      limit: int = 1000) -> list[dict]:
        """CDX records whose SURT == key (or startswith key when prefix=True)."""
        idx = await self.cluster(client, crawl_id)
        out: list[dict] = []
        i = idx.start(key)
        while i < len(idx.blocks) and len(out) < limit:
            if i > idx.start(key) and idx.keys[i].split(" ", 1)[0] > key and not (
                    prefix and idx.keys[i].startswith(key)):
                break
            lines = await self._block(client, crawl_id, *idx.blocks[i])
            past = False
            for line in lines:
                surt_key, _, rest = line.partition(" ")
                hit = surt_key.startswith(key) if prefix else surt_key == key
                if hit:
                    ts, _, js = rest.partition(" ")
                    try:
                        rec = json.loads(js)
                    except json.JSONDecodeError:
                        continue
                    rec["timestamp"], rec["urlkey"] = ts, surt_key
                    out.append(rec)
                    if len(out) >= limit:
                        break
                elif surt_key > key and not (prefix and surt_key.startswith(key)):
                    past = True
                    break
            if past:
                break
            i += 1
        return out

    async def warm(self, snapshot: str) -> None:
        """Download the cluster.idx files for the crawls a snapshot uses, ahead of traffic."""
        try:
            async with _client() as client:
                for crawl in await self.crawls_before(client, snapshot):
                    await self.cluster(client, crawl["id"])
        except Exception as e:  # lookups will retry lazily
            import logging
            logging.getLogger("frozen").warning("commoncrawl warm-up failed: %s", e)

    # ---- lookup ------------------------------------------------------------
    async def lookup(self, url: str, snapshot: str) -> Page | None:
        key = surt(url)
        cutoff = snapshot + "235959"
        async with _client() as client:
            for crawl in await self.crawls_before(client, snapshot):
                recs = await self.records(client, crawl["id"], key)
                # SURT keys fold case and trailing slashes, so keep only captures of this exact
                # page, and only real responses (not the crawler being blocked or rate-limited).
                recs = [x for x in recs if x["timestamp"] <= cutoff and same_page(x.get("url", ""), url)
                        and usable_status(x.get("status"))]
                if not recs:
                    continue
                rec = max(recs, key=lambda x: (x.get("url") == url, x.get("status") == "200", x["timestamp"]))
                return await self.fetch_record(client, rec, url)
        return None

    async def fetch_record(self, client: httpx.AsyncClient, rec: dict, url: str) -> Page:
        off, length = int(rec["offset"]), int(rec["length"])
        async with self._sem:
            r = await client.get(self.DATA + rec["filename"],
                                 headers={"Range": f"bytes={off}-{off + length - 1}"})
        if r.status_code not in (200, 206):
            raise SourceError(f"cc data {r.status_code}")
        page = parse_warc_record(r.content, url)
        page.captured = rec["timestamp"]
        page.source = self.name
        return page


def parse_warc_record(raw: bytes, url: str) -> Page:
    """Parse one gzipped WARC response record into a Page (decoding chunked/gzip bodies)."""
    for record in ArchiveIterator(io.BytesIO(raw)):
        if record.rec_type != "response":
            continue
        status = int(record.http_headers.get_statuscode() or 0)
        headers = list(record.http_headers.headers)
        body = record.content_stream().read(config.MAX_BODY)
        captured = (record.rec_headers.get_header("WARC-Date") or "")
        captured = re.sub(r"\D", "", captured)[:14]
        return Page(url, status, headers, body, "warc", captured)
    raise SourceError("no response record")


# ---------------------------------------------------------------------------
# Wayback Machine: CDX for the last capture <= cutoff, then raw (id_) replay
# ---------------------------------------------------------------------------
class Wayback:
    name = "wayback"
    CDX = "https://web.archive.org/cdx/search/cdx"
    WEB = "https://web.archive.org/web/"
    _redir = re.compile(r"^https?://web\.archive\.org/web/(\d{1,14})(?:[a-z_]+)?/(.+)$")

    def __init__(self):
        self._sem = asyncio.Semaphore(4)

    async def lookup(self, url: str, snapshot: str) -> Page | None:
        cutoff = snapshot + "235959"
        async with _client() as client, self._sem:
            r = await client.get(self.CDX, params={
                "url": url, "to": cutoff, "output": "json", "limit": "-50",
                "fl": "timestamp,original,statuscode",
                "filter": "statuscode:(200|301|302|303|307|308|404)",
            })
            if r.status_code != 200:
                raise SourceError(f"wayback cdx {r.status_code}")
            rows = r.json() if r.text.strip() else []
            rows = [x for x in rows[1:]  # first row is the header
                    if x and x[0] <= cutoff and same_page(x[1], url) and usable_status(x[2])]
            if not rows:
                return None
            ts, original, _ = rows[-1]
            r = await client.get(f"{self.WEB}{ts}id_/{original}")
        memento = r.headers.get("memento-datetime")
        captured = ts
        if r.status_code >= 500 or r.status_code == 429:
            raise SourceError(f"wayback replay {r.status_code}")
        headers = [("Content-Type", r.headers.get("content-type", "application/octet-stream"))]
        status = r.status_code
        if 300 <= status < 400:
            loc = r.headers.get("location", "")
            m = self._redir.match(loc)
            if m:
                if m.group(1) > cutoff:
                    return None  # replay drifted past the cutoff; refuse to leak
                loc = m.group(2)
            headers.append(("Location", loc))
        for k, v in r.headers.items():
            if k.lower().startswith("x-archive-orig-"):
                name = k[len("x-archive-orig-"):]
                if name.lower() in ("cache-control", "last-modified", "content-language", "link"):
                    headers.append((name, v))
        if memento:
            headers.append(("X-Frozen-Memento", memento))
        return Page(url, status, headers, r.content[:config.MAX_BODY], self.name, captured)


_WWW_RE = re.compile(r"^www\d*\.")


def same_page(a: str, b: str) -> bool:
    """True if two URLs are the same page, ignoring scheme, www., default ports and query order.
    Path case and trailing slashes matter (Wikipedia's /wiki/X and /wiki/X/ are different pages)."""
    def norm(u: str):
        p = urlsplit(u if "://" in u else "http://" + u)
        host = _WWW_RE.sub("", (p.hostname or "").lower().rstrip("."))
        port = p.port if p.port not in (None, 80, 443) else None
        q = "&".join(sorted(x for x in p.query.split("&") if x))
        return host, port, unquote(p.path or "/"), unquote(q)
    try:
        return norm(a) == norm(b)
    except ValueError:
        return False


def usable_status(status) -> bool:
    """Real page responses: 2xx, redirects, and genuine 'not found'. Other 4xx/5xx captures are
    almost always the crawler being blocked (403/406/429) or a server error."""
    s = str(status or "")
    return s.isdigit() and (s[0] in "23" or s in ("404", "410"))


def build_sources(names: list[str]):
    table = {"commoncrawl": CommonCrawl, "wayback": Wayback}
    return [table[n]() for n in names if n in table]
