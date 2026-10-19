"""Seed the frozen web (cache + search index) from Common Crawl.

Pages agents visit are indexed automatically, but search is only as good as the corpus,
so seed the domains your agents will need:

  python -m frozen.ingest --domain en.wikipedia.org --limit 5000
  python -m frozen.ingest --domain arxiv.org --domain docs.python.org --limit 2000 --snapshot 20250601
  python -m frozen.ingest --urls urls.txt
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from . import config
from .sources import CommonCrawl, UA, Wayback
from .store import Store
from .surt import surt


async def list_domain(cc: CommonCrawl, client, crawl: dict, domain: str, limit: int) -> list[dict]:
    """HTML pages captured with status 200 under a domain (read from the ZipNum index)."""
    prefix = surt(f"http://{domain}/")[:-1]  # "org,wikipedia,en)" -> matches every path
    recs = await cc.records(client, crawl["id"], prefix, prefix=True, limit=limit * 4)
    best: dict[str, dict] = {}
    for rec in recs:
        if rec.get("status") != "200" or "html" not in rec.get("mime", ""):
            continue
        if rec["url"] not in best or rec["timestamp"] > best[rec["url"]]["timestamp"]:
            best[rec["url"]] = rec
    return list(best.values())[:limit]


async def run(args) -> None:
    store = Store(config.DATA_DIR / "frozen.sqlite")
    cc = CommonCrawl()
    sem = asyncio.Semaphore(args.concurrency)
    done = failed = 0

    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": UA}) as client:
        crawls = await cc.crawls_before(client, args.snapshot)
        if args.crawl:
            crawls = [c for c in cc._collinfo if c["id"] == args.crawl]
        if not crawls:
            sys.exit(f"no Common Crawl crawl finished on or before {args.snapshot}")
        crawl = crawls[0]
        print(f"using {crawl['id']} ({crawl.get('name', '')}) for snapshot {args.snapshot}")

        async def ingest_rec(rec):
            nonlocal done, failed
            async with sem:
                if store.get_page(args.snapshot, rec["url"]):
                    done += 1
                    return
                try:
                    page = await cc.fetch_record(client, rec, rec["url"])
                    store.put_page(args.snapshot, page)
                    done += 1
                except Exception as e:
                    failed += 1
                    print(f"  ! {rec['url']}: {e}", file=sys.stderr)
                if (done + failed) % 100 == 0:
                    print(f"  {done} stored, {failed} failed")

        recs: list[dict] = []
        for d in args.domain:
            got = await list_domain(cc, client, crawl, d, args.limit)
            print(f"{d}: {len(got)} pages listed")
            recs += got

        if args.urls:
            wb = Wayback()
            urls = [u.strip() for u in open(args.urls) if u.strip().startswith("http")]
            for u in urls:
                try:
                    page = await cc.lookup(u, args.snapshot) or await wb.lookup(u, args.snapshot)
                except Exception as e:
                    page = None
                    print(f"  ! {u}: {e}", file=sys.stderr)
                if page:
                    page.url = u
                    store.put_page(args.snapshot, page)
                    done += 1
                else:
                    failed += 1

        await asyncio.gather(*(ingest_rec(r) for r in recs))
    print(f"done: {done} stored, {failed} failed; index now has {store.doc_count()} docs")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--domain", action="append", default=[], help="domain to seed (repeatable)")
    p.add_argument("--urls", help="file with one URL per line")
    p.add_argument("--limit", type=int, default=1000, help="max pages per domain")
    p.add_argument("--snapshot", default=config.DEFAULT_SNAPSHOT, help="YYYYMMDD cutoff")
    p.add_argument("--crawl", help="force a Common Crawl id, e.g. CC-MAIN-2026-21")
    p.add_argument("--concurrency", type=int, default=8)
    args = p.parse_args()
    if not args.domain and not args.urls:
        p.error("give --domain and/or --urls")
    if not config.valid_snapshot(args.snapshot):
        p.error("--snapshot must be YYYYMMDD")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
