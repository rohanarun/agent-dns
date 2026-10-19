"""SQLite store: page cache (pins every response per snapshot), full-text index, request log,
and per-client snapshot assignments."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS pages (
    snapshot TEXT NOT NULL,
    url TEXT NOT NULL,
    status INTEGER NOT NULL,
    headers TEXT NOT NULL,
    body BLOB NOT NULL,
    source TEXT NOT NULL,
    captured TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (snapshot, url)
);
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    captured TEXT NOT NULL,
    UNIQUE (url, captured)
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(title, body, content='', tokenize='porter');
CREATE TABLE IF NOT EXISTS log (
    ts REAL NOT NULL,
    client TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    status INTEGER NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_client ON log(client, ts);
CREATE TABLE IF NOT EXISTS snippets (id INTEGER PRIMARY KEY, title TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS clients (
    ip TEXT PRIMARY KEY,
    snapshot TEXT NOT NULL,
    label TEXT
);
"""

# Headers we never replay: they describe the original transfer, not the content.
DROP_HEADERS = {
    "content-encoding", "transfer-encoding", "content-length", "connection",
    "keep-alive", "set-cookie", "strict-transport-security", "alt-svc",
    "public-key-pins", "report-to", "nel", "expect-ct",
}


@dataclass
class Page:
    url: str
    status: int
    headers: list[tuple[str, str]]
    body: bytes
    source: str
    captured: str  # YYYYMMDDhhmmss of the archive capture
    extra: dict = field(default_factory=dict)

    def clean_headers(self) -> list[tuple[str, str]]:
        return [(k, v) for k, v in self.headers if k.lower() not in DROP_HEADERS]

    def content_type(self) -> str:
        for k, v in self.headers:
            if k.lower() == "content-type":
                return v
        return ""


def extract_text(html: bytes, limit: int = 200_000) -> tuple[str, str]:
    soup = BeautifulSoup(html[:2_000_000], "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:300]
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:limit]
    return title, text


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self._reindex = False
            self._migrate()
        if self._reindex:
            with self.lock:
                rows = self.db.execute("SELECT url, headers, body, captured FROM pages WHERE status=200").fetchall()
            for url, headers, body, captured in rows:
                if any(k.lower() == "content-type" and "html" in v.lower() for k, v in json.loads(headers)):
                    self.index(url, zlib.decompress(body), captured)

    # Bump when lookup logic changes in a way that makes old pinned results wrong.
    CACHE_VERSION = 4

    def _migrate(self) -> None:
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        row = self.db.execute("SELECT v FROM meta WHERE k='cache_version'").fetchone()
        if row is None or int(row[0]) < self.CACHE_VERSION:
            # v2/v3: Common Crawl captures could be a different page (trailing slash) or a block page.
            if row is None or int(row[0]) < 3:
                self.db.execute("DELETE FROM pages WHERE source IN ('commoncrawl', 'miss')")
            # v4: the search index keeps one entry per (url, capture), so each snapshot only ever
            # sees text from captures on or before its date. Rebuild it from the page cache.
            self.db.executescript("DROP TABLE IF EXISTS docs_fts; DROP TABLE IF EXISTS docs; "
                                  "DROP TABLE IF EXISTS snippets;")
            self.db.executescript(SCHEMA)
            self._reindex = True
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('cache_version', ?)",
                            (str(self.CACHE_VERSION),))

    # ---- page cache -------------------------------------------------------
    def get_page(self, snapshot: str, url: str) -> Page | None:
        with self.lock:
            row = self.db.execute(
                "SELECT status, headers, body, source, captured FROM pages WHERE snapshot=? AND url=?",
                (snapshot, url),
            ).fetchone()
        if not row:
            return None
        status, headers, body, source, captured = row
        return Page(url, status, [tuple(h) for h in json.loads(headers)], zlib.decompress(body), source, captured)

    def put_page(self, snapshot: str, page: Page) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?)",
                (snapshot, page.url, page.status, json.dumps(page.headers),
                 zlib.compress(page.body, 6), page.source, page.captured, time.time()),
            )
        if page.status == 200 and "html" in page.content_type().lower():
            try:
                self.index(page.url, page.body, page.captured)
            except sqlite3.Error as e:  # search is best-effort; never fail serving the page
                logging.getLogger("frozen").warning("index failed for %s: %s", page.url, e)

    # ---- search -----------------------------------------------------------
    def index(self, url: str, html: bytes, captured: str) -> None:
        try:
            title, text = extract_text(html)
        except Exception:
            return
        if not text:
            return
        with self.lock:
            if self.db.execute("SELECT 1 FROM docs WHERE url=? AND captured=?", (url, captured)).fetchone():
                return
            doc_id = self.db.execute(
                "INSERT INTO docs(url, captured) VALUES (?,?)", (url, captured)).lastrowid
            self.db.execute("INSERT INTO docs_fts(rowid, title, body) VALUES (?,?,?)", (doc_id, title, text))
            # contentless FTS can't return text, so keep a short snippet source alongside
            self.db.execute("INSERT OR REPLACE INTO snippets VALUES (?,?,?)", (doc_id, title, text[:2000]))

    def search(self, query: str, snapshot: str, limit: int = 10, offset: int = 0) -> list[dict]:
        terms = re.findall(r"[\w]+", query.lower())
        if not terms:
            return []
        match = " ".join(f'"{t}"' for t in terms[:12])
        cutoff = snapshot + "999999"
        with self.lock:
            rows = self.db.execute(
                """SELECT d.url, d.captured, s.title, s.text FROM docs_fts f
                   JOIN docs d ON d.id = f.rowid JOIN snippets s ON s.id = d.id
                   WHERE docs_fts MATCH ? AND d.captured <= ?
                   ORDER BY bm25(docs_fts, 3.0, 1.0) LIMIT ?""",
                (match, cutoff, (limit + offset) * 4),
            ).fetchall()
        # One result per URL: its newest capture on or before the cutoff.
        best: dict[str, tuple] = {}
        order: list[str] = []
        for url, captured, title, text in rows:
            if url not in best:
                order.append(url)
            if url not in best or captured > best[url][0]:
                best[url] = (captured, title, text)
        out = []
        for url in order[offset:offset + limit]:
            captured, title, text = best[url]
            out.append({"url": url, "title": title or url, "captured": captured,
                        "snippet": _snippet(text, terms)})
        return out

    def doc_count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT count(*) FROM docs").fetchone()[0]

    # ---- request log ------------------------------------------------------
    def log(self, client: str, snapshot: str, method: str, url: str, status: int, source: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO log VALUES (?,?,?,?,?,?,?)",
                            (time.time(), client, snapshot, method, url, status, source))

    def recent_log(self, client: str | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT ts, client, snapshot, method, url, status, source FROM log"
        args: tuple = ()
        if client:
            q += " WHERE client=?"
            args = (client,)
        q += " ORDER BY ts DESC LIMIT ?"
        with self.lock:
            rows = self.db.execute(q, args + (limit,)).fetchall()
        keys = ["ts", "client", "snapshot", "method", "url", "status", "source"]
        return [dict(zip(keys, r)) for r in rows]

    # ---- per-client snapshots --------------------------------------------
    def client_snapshot(self, ip: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT snapshot FROM clients WHERE ip=?", (ip,)).fetchone()
        return row[0] if row else None

    def set_client(self, ip: str, snapshot: str | None, label: str = "") -> None:
        with self.lock:
            if snapshot is None:
                self.db.execute("DELETE FROM clients WHERE ip=?", (ip,))
            else:
                self.db.execute("INSERT OR REPLACE INTO clients VALUES (?,?,?)", (ip, snapshot, label))

    def clients(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT ip, snapshot, label FROM clients ORDER BY ip").fetchall()
        return [{"ip": a, "snapshot": b, "label": c} for a, b, c in rows]

    def stats(self) -> dict:
        with self.lock:
            pages = self.db.execute("SELECT count(*) FROM pages").fetchone()[0]
            reqs = self.db.execute("SELECT count(*) FROM log").fetchone()[0]
        return {"cached_pages": pages, "indexed_docs": self.doc_count(), "requests_logged": reqs}


def _snippet(text: str, terms: list[str], width: int = 240) -> str:
    low = text.lower()
    pos = min((low.find(t) for t in terms if low.find(t) >= 0), default=0)
    start = max(0, pos - width // 3)
    s = text[start:start + width]
    return ("…" if start else "") + s + ("…" if start + width < len(text) else "")
