"""Offline unit tests: store, search cutoff, WARC parsing, crawl selection, archive sources
(with mocked HTTP), DNS resolver."""
import asyncio
import gzip
import io
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frozen import sources  # noqa: E402
from frozen.dns_server import WildcardResolver  # noqa: E402
from frozen.sources import CommonCrawl, Wayback, parse_warc_record  # noqa: E402
from frozen.store import Page, Store  # noqa: E402

HTML_OLD = b"<html><head><title>Kappa bounds 2024</title></head><body>integer multiplication exponent kappa result</body></html>"
HTML_NEW = b"<html><head><title>Kappa solved 2026</title></head><body>integer multiplication kappa final answer</body></html>"


def make_warc(url: str, body: bytes, status: int = 200, date="2025-03-04T05:06:07Z", gz_body=False) -> bytes:
    payload = gzip.compress(body) if gz_body else body
    http = (f"HTTP/1.1 {status} OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            + ("Content-Encoding: gzip\r\n" if gz_body else "")
            + f"Content-Length: {len(payload)}\r\nSet-Cookie: a=b\r\n\r\n").encode() + payload
    rec = (f"WARC/1.0\r\nWARC-Type: response\r\nWARC-Target-URI: {url}\r\nWARC-Date: {date}\r\n"
           f"WARC-Record-ID: <urn:uuid:1>\r\nContent-Type: application/http; msgtype=response\r\n"
           f"Content-Length: {len(http)}\r\n\r\n").encode() + http + b"\r\n\r\n"
    return gzip.compress(rec)


def test_store_cache_and_search_cutoff(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.put_page("20250101", Page("https://a.org/old", 200, [("Content-Type", "text/html")], HTML_OLD, "t", "20240101000000"))
    s.put_page("20270101", Page("https://a.org/new", 200, [("Content-Type", "text/html")], HTML_NEW, "t", "20260901000000"))
    assert s.get_page("20250101", "https://a.org/old").body == HTML_OLD
    assert s.get_page("20250101", "https://a.org/new") is None
    early = [r["url"] for r in s.search("kappa", "20250101")]
    late = [r["url"] for r in s.search("kappa", "20270101")]
    assert early == ["https://a.org/old"]          # nothing after the cutoff leaks into search
    assert set(late) == {"https://a.org/old", "https://a.org/new"}
    assert s.search("", "20270101") == []
    assert "kappa" in s.search("kappa", "20250101")[0]["snippet"].lower()


def test_clean_headers_drops_transfer_and_cookies():
    p = Page("u", 200, [("Content-Encoding", "gzip"), ("Set-Cookie", "x"), ("Content-Type", "text/html"),
                         ("Strict-Transport-Security", "max-age=1")], b"", "t", "")
    assert p.clean_headers() == [("Content-Type", "text/html")]


@pytest.mark.parametrize("gz", [False, True])
def test_parse_warc_record(gz):
    p = parse_warc_record(make_warc("https://x.org/", HTML_OLD, gz_body=gz), "https://x.org/")
    assert p.status == 200 and p.body == HTML_OLD and p.captured == "20250304050607"
    assert "text/html" in p.content_type()


COLLINFO = [
    {"id": "CC-MAIN-2026-30", "from": "2026-07-01T00:00:00", "to": "2026-07-15T00:00:00", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-30-index"},
    {"id": "CC-MAIN-2026-21", "from": "2026-05-10T00:00:00", "to": "2026-05-25T00:00:00", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-21-index"},
    {"id": "CC-MAIN-2026-08", "from": "2026-02-10T00:00:00", "to": "2026-02-25T00:00:00", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-08-index"},
]


def test_select_crawls_respects_cutoff():
    ids = [c["id"] for c in CommonCrawl.select_crawls(COLLINFO, "20260601", 2)]
    assert ids == ["CC-MAIN-2026-21", "CC-MAIN-2026-08"]
    assert CommonCrawl.select_crawls(COLLINFO, "20200101", 2) == []


def _mock(handler, monkeypatch):
    monkeypatch.setattr(sources, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def build_zipnum(records, per_block=3):
    """records: [(surt, ts, json_dict)] -> (cluster.idx text, shard bytes) in Common Crawl's
    ZipNum layout: the shard is concatenated gzip members, one per block."""
    records = sorted(records, key=lambda r: (r[0], r[1]))
    shard, idx = b"", []
    for b in range(0, len(records), per_block):
        chunk = records[b:b + per_block]
        member = gzip.compress("".join(f"{s} {t} {json.dumps(j)}\n" for s, t, j in chunk).encode())
        idx.append(f"{chunk[0][0]} {chunk[0][1]}\tcdx-00000.gz\t{len(shard)}\t{len(member)}\t{b // per_block + 1}")
        shard += member
    return "\n".join(idx) + "\n", shard


def _cc_server(warc, records, seen, collinfo_status=200):
    cluster, shard = build_zipnum(records)

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        if req.url.path == "/collinfo.json":
            return httpx.Response(collinfo_status, json=COLLINFO if collinfo_status == 200 else None)
        assert req.url.host == "data.commoncrawl.org", "the overloaded CDX server must not be used"
        assert "2026-30" not in req.url.path  # crawl after the cutoff must never be read
        if req.url.path.endswith("/CC-MAIN-2026-21/indexes/cluster.idx"):
            return httpx.Response(200, content=cluster.encode())
        if req.url.path.endswith("/indexes/cluster.idx"):
            return httpx.Response(200, content=b"")
        a, b = map(int, req.headers["range"].split("=")[1].split("-"))
        if req.url.path.endswith("/CC-MAIN-2026-21/indexes/cdx-00000.gz"):
            return httpx.Response(206, content=shard[a:b + 1])
        if req.url.path.endswith("/a.warc.gz"):
            assert (a, b) == (100, 100 + len(warc) - 1)
            return httpx.Response(206, content=warc)
        return httpx.Response(404)
    return handler


def _cc_records(warc):
    rec = lambda url, st="200", ts="20260512000000": {
        "url": url, "status": st, "mime": "text/html", "filename": "crawl-data/a.warc.gz",
        "offset": "100", "length": str(len(warc))}
    rs = [(f"com,aaa{i})/", "20260510000000", rec(f"https://aaa{i}.com/")) for i in range(5)]
    # the target spans a block boundary; the newest 200 capture must win
    rs += [("org,x)/p", "20260511000000", rec("https://x.org/p")),
           ("org,x)/p", "20260512000000", rec("https://x.org/p")),
           ("org,x)/p", "20260513000000", rec("https://x.org/p", st="503")),
           ("org,x)/p/q", "20260512000000", rec("https://x.org/p/q")),
           ("org,x)/pz", "20260512000000", rec("https://x.org/pz"))]
    rs += [(f"org,zzz{i})/", "20260510000000", rec(f"https://zzz{i}.org/")) for i in range(5)]
    return rs


def test_commoncrawl_lookup_via_zipnum(monkeypatch, tmp_path):
    warc = make_warc("https://x.org/p", HTML_OLD)
    seen = []
    _mock(_cc_server(warc, _cc_records(warc), seen), monkeypatch)
    cc = CommonCrawl(cache_dir=tmp_path)
    page = asyncio.run(cc.lookup("https://www.X.org/p", "20260601"))
    assert page.body == HTML_OLD and page.captured == "20260512000000" and page.source == "commoncrawl"
    assert (tmp_path / "CC-MAIN-2026-21" / "cluster.idx").exists()
    assert asyncio.run(cc.lookup("https://x.org/nope", "20260601")) is None
    # cluster.idx is downloaded once, then reused
    assert sum("2026-21/indexes/cluster.idx" in u for u in seen) == 1


def test_commoncrawl_prefix_scan(monkeypatch, tmp_path):
    warc = make_warc("https://x.org/p", HTML_OLD)
    _mock(_cc_server(warc, _cc_records(warc), []), monkeypatch)
    cc = CommonCrawl(cache_dir=tmp_path)

    async def go():
        async with sources._client() as c:
            return await cc.records(c, "CC-MAIN-2026-21", "org,x)", prefix=True)
    urls = sorted({r["url"] for r in asyncio.run(go())})
    assert urls == ["https://x.org/p", "https://x.org/p/q", "https://x.org/pz"]


def test_commoncrawl_survives_collinfo_outage(monkeypatch, tmp_path):
    warc = make_warc("https://x.org/p", HTML_OLD)
    _mock(_cc_server(warc, _cc_records(warc), []), monkeypatch)
    asyncio.run(CommonCrawl(cache_dir=tmp_path).lookup("https://x.org/p", "20260601"))
    _mock(_cc_server(warc, _cc_records(warc), [], collinfo_status=504), monkeypatch)
    page = asyncio.run(CommonCrawl(cache_dir=tmp_path).lookup("https://x.org/p", "20260601"))
    assert page.body == HTML_OLD


def test_surt_matches_common_crawl_keys():
    from frozen.surt import surt
    assert surt("https://en.wikipedia.org/wiki/Integer_Multiplication") == "org,wikipedia,en)/wiki/integer_multiplication"
    assert surt("http://www2.Example.com:80/A/B?z=1&a=2#f") == "com,example)/a/b?a=2&z=1"
    assert surt("https://example.com") == "com,example)/"
    assert surt("http://user:pw@x.org/%7Efoo/") == "org,x)/~foo"
    assert surt("http://example.com:8080/a//b/./c/../d") == "com,example:8080)/a/b/d"
    assert surt("http://165.22.100.0/robots.txt") == "0,100,22,165)/robots.txt"


def test_wayback_lookup_and_cutoff(monkeypatch):
    def handler(req: httpx.Request):
        if req.url.path.startswith("/cdx"):
            assert req.url.params["to"] == "20250101235959"
            return httpx.Response(200, json=[["timestamp", "original", "statuscode"],
                                             ["20241201000000", "https://y.org/", "200"]])
        if req.url.path.startswith("/web/20241201000000id_/"):
            return httpx.Response(200, content=HTML_OLD, headers={
                "content-type": "text/html", "x-archive-orig-last-modified": "Mon"})
        return httpx.Response(500)

    _mock(handler, monkeypatch)
    page = asyncio.run(Wayback().lookup("https://y.org/", "20250101"))
    assert page.body == HTML_OLD and page.captured == "20241201000000"
    assert ("last-modified", "Mon") in [(k.lower(), v) for k, v in page.headers]


def test_wayback_refuses_redirect_past_cutoff(monkeypatch):
    def handler(req: httpx.Request):
        if req.url.path.startswith("/cdx"):
            return httpx.Response(200, json=[["timestamp", "original", "statuscode"],
                                             ["20241201000000", "https://y.org/", "302"]])
        return httpx.Response(302, headers={"location": "https://web.archive.org/web/20260101000000id_/https://y.org/new"})

    _mock(handler, monkeypatch)
    assert asyncio.run(Wayback().lookup("https://y.org/", "20250101")) is None


def test_wayback_redirect_unwrapped(monkeypatch):
    def handler(req: httpx.Request):
        if req.url.path.startswith("/cdx"):
            return httpx.Response(200, json=[["timestamp", "original", "statuscode"],
                                             ["20241201000000", "http://y.org/", "301"]])
        return httpx.Response(301, headers={"location": "https://web.archive.org/web/20241201000001id_/https://y.org/"})

    _mock(handler, monkeypatch)
    p = asyncio.run(Wayback().lookup("http://y.org/", "20250101"))
    assert p.status == 301 and ("Location", "https://y.org/") in p.headers


def test_wayback_no_capture(monkeypatch):
    _mock(lambda req: httpx.Response(200, text=""), monkeypatch)
    assert asyncio.run(Wayback().lookup("https://z.org/", "20250101")) is None


def test_dns_resolver():
    from dnslib import DNSRecord, QTYPE

    class H:
        client_address = ("1.2.3.4", 5555)

    r = WildcardResolver("34.1.2.3", rate=0.0001, burst=3)
    a = r.resolve(DNSRecord.question("anything.example.com", "A"), H())
    assert str(a.rr[0].rdata) == "34.1.2.3"
    aaaa = r.resolve(DNSRecord.question("anything.example.com", "AAAA"), H())
    assert aaaa.rr == [] and aaaa.header.rcode == 0
    r.resolve(DNSRecord.question("x.com", "A"), H())
    limited = r.resolve(DNSRecord.question("x.com", "A"), H())
    assert limited.header.rcode == 5  # REFUSED once the bucket is empty


def test_commoncrawl_skips_blocked_and_wrong_page(monkeypatch, tmp_path):
    warc = make_warc("https://x.org/p", HTML_OLD)
    rec = lambda url, st: {"url": url, "status": st, "mime": "text/html", "filename": "crawl-data/a.warc.gz",
                           "offset": "100", "length": str(len(warc))}
    records = [(f"com,aaa{i})/", "20260510000000", rec(f"https://aaa{i}.com/", "200")) for i in range(4)]
    records += [("org,x)/turing", "20260512000000", rec("https://x.org/Turing/", "404")),   # other page
                ("org,x)/blocked", "20260512000000", rec("https://x.org/blocked", "403")),  # crawler blocked
                ("org,x)/gone", "20260512000000", rec("https://x.org/gone", "404"))]        # real 404
    _mock(_cc_server(warc, records, []), monkeypatch)
    cc = CommonCrawl(cache_dir=tmp_path)
    assert asyncio.run(cc.lookup("https://x.org/Turing", "20260601")) is None
    assert asyncio.run(cc.lookup("https://x.org/blocked", "20260601")) is None
    assert asyncio.run(cc.lookup("https://x.org/gone", "20260601")) is not None


def test_cache_migration_purges_old_commoncrawl_rows(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.put_page("20260601", Page("https://a.org/", 403, [], b"x", "commoncrawl", "20260501000000"))
    s.put_page("20260601", Page("https://b.org/", 200, [], b"y", "wayback", "20260501000000"))
    s.db.execute("UPDATE meta SET v='1' WHERE k='cache_version'")
    s.put_page("20260601", Page("https://c.org/", 200, [("Content-Type", "text/html")],
                                b"<html><body>reindexed zebra</body></html>", "wayback", "20260501000000"))
    s2 = Store(tmp_path / "db.sqlite")
    assert s2.get_page("20260601", "https://a.org/") is None
    assert s2.get_page("20260601", "https://b.org/") is not None
    assert [r["url"] for r in s2.search("zebra", "20260601")] == ["https://c.org/"]  # index rebuilt


def test_http_rate_limiter():
    from frozen.core import RateLimiter
    rl = RateLimiter(rate=0.0001, burst=3)
    assert [rl.allow("1.1.1.1") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("2.2.2.2")  # per IP
    assert all(RateLimiter(0, 1).allow("x") for _ in range(10))  # 0 = unlimited


def test_search_never_shows_text_from_after_the_cutoff(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    new = b"<html><title>Page</title><body>kappa solved spoiler answer</body></html>"
    old = b"<html><title>Page</title><body>kappa open problem</body></html>"
    s.put_page("20270101", Page("https://a.org/", 200, [("Content-Type", "text/html")], new, "t", "20260901000000"))
    s.put_page("20250101", Page("https://a.org/", 200, [("Content-Type", "text/html")], old, "t", "20240101000000"))
    early = s.search("kappa", "20250101")
    assert len(early) == 1 and "spoiler" not in early[0]["snippet"] and "open problem" in early[0]["snippet"]
    assert s.search("spoiler", "20250101") == []
    late = s.search("kappa", "20270101")
    assert len(late) == 1 and "spoiler" in late[0]["snippet"]
    # reopening (migration already applied) keeps the index intact
    assert len(Store(tmp_path / "db.sqlite").search("kappa", "20270101")) == 1
