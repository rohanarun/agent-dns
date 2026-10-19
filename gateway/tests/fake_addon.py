# mitmdump addon for integration tests: the real FrozenWeb with an in-memory fake archive.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frozen.core import FrozenWeb  # noqa: E402
from frozen.store import Page  # noqa: E402

ARCHIVE = {
    "https://example.com/": ("20250301000000", 200, "text/html",
        b"<html><head><title>Example Domain</title></head><body>archived example page about frozen kappa</body></html>"),
    "https://example.com/new": ("20260901000000", 200, "text/html", b"<html>from the future</html>"),
    "http://old.example.org/": ("20200101000000", 301, "text/html", b""),
    "https://arxiv.org/abs/1": ("20241105000000", 200, "text/html",
        b"<html><head><title>Integer multiplication in time O(n log n)</title></head><body>kappa exponent bound</body></html>"),
}


class FakeSource:
    name = "fake"

    def __init__(self):
        self.calls = 0

    async def lookup(self, url, snapshot):
        self.calls += 1
        if url not in ARCHIVE:
            return None
        captured, status, ctype, body = ARCHIVE[url]
        if captured > snapshot + "235959":
            return None
        headers = [("Content-Type", ctype), ("Content-Encoding", "gzip")]
        if status == 301:
            headers.append(("Location", "https://example.com/"))
        return Page(url, status, headers, body, "fake", captured)


addons = [FrozenWeb(sources=[FakeSource()])]
