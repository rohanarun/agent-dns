"""The "easy DNS": answers every A query with the gateway's public IP.

AAAA and everything else get an empty NOERROR answer, so clients fall back to IPv4 and never
learn a real address. A small per-client rate limit keeps it from being useful for abuse.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict

from dnslib import QTYPE, RCODE, RR, A
from dnslib.server import BaseResolver, DNSLogger, DNSServer

from . import config

log = logging.getLogger("frozen.dns")


class WildcardResolver(BaseResolver):
    def __init__(self, ip: str, ttl: int = 300, rate: float = 200.0, burst: int = 400):
        self.ip, self.ttl = ip, ttl
        self.rate, self.burst = rate, burst
        self._buckets: dict[str, list[float]] = defaultdict(lambda: [float(burst), time.monotonic()])
        self._lock = threading.Lock()

    def _allow(self, client: str) -> bool:
        with self._lock:
            if len(self._buckets) > 100_000:  # open resolver: don't grow forever
                self._buckets.clear()
            tokens, last = self._buckets[client]
            now = time.monotonic()
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._buckets[client] = [tokens, now]
                return False
            self._buckets[client] = [tokens - 1, now]
            return True

    def resolve(self, request, handler):
        reply = request.reply()
        client = handler.client_address[0] if handler and handler.client_address else "?"
        if not self._allow(client):
            reply.header.rcode = RCODE.REFUSED
            return reply
        q = request.q
        if q.qtype in (QTYPE.A, QTYPE.ANY):
            reply.add_answer(RR(q.qname, QTYPE.A, ttl=self.ttl, rdata=A(self.ip)))
        return reply  # NOERROR, no data for AAAA/MX/TXT/...


class QuietLogger(DNSLogger):
    def __init__(self):
        super().__init__(log="-request,-reply,-truncated,-data,+error", prefix=False)


def main():
    logging.basicConfig(level=logging.INFO)
    bind = os.environ.get("FROZEN_DNS_BIND", "0.0.0.0")
    port = int(os.environ.get("FROZEN_DNS_PORT", "53"))
    resolver = WildcardResolver(config.PUBLIC_IP, rate=config.DNS_RATE, burst=config.DNS_BURST)
    servers = [DNSServer(resolver, address=bind, port=port, logger=QuietLogger()),
               DNSServer(resolver, address=bind, port=port, tcp=True, logger=QuietLogger())]
    for s in servers:
        s.start_thread()
    log.info("DNS on %s:%s -> every name resolves to %s", bind, port, config.PUBLIC_IP)
    while all(s.isAlive() for s in servers):
        time.sleep(5)
    raise SystemExit("DNS server thread died")


if __name__ == "__main__":
    main()
