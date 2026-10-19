"""Runtime configuration, all from environment variables."""
import os
from pathlib import Path


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip().lower() for x in os.environ.get(name, default).split(",") if x.strip()]


# Public IP every hostname resolves to (the VM's static external IP).
PUBLIC_IP = os.environ.get("FROZEN_PUBLIC_IP", "127.0.0.1")

# Default "as of" date for the frozen web, YYYYMMDD. Nothing newer is ever served.
DEFAULT_SNAPSHOT = os.environ.get("FROZEN_SNAPSHOT", "20260601")

# Where the cache, search index, logs and CA live.
DATA_DIR = Path(os.environ.get("FROZEN_DATA_DIR", "/data"))

# Token required for /admin endpoints on the control host.
ADMIN_TOKEN = os.environ.get("FROZEN_ADMIN_TOKEN", "")

# Hostnames that pass through to the real internet (e.g. pypi.org). Empty = full isolation.
PASSTHROUGH = _list("FROZEN_PASSTHROUGH")

# Which archive sources to try, in order.
SOURCES = _list("FROZEN_SOURCES", "commoncrawl,wayback")

# Real public domains for the control plane (e.g. tensorbook.com,www.tensorbook.com).
# These get a publicly trusted Let's Encrypt cert, so ordinary browsers see a valid lock.
SITE_DOMAINS = _list("FROZEN_SITE_DOMAINS")

# Control-plane hostname (status, CA download, JSON API, admin).
CONTROL_HOSTS = ({"frozen.internal", "frozen.web"} | set(_list("FROZEN_CONTROL_HOSTS"))
                 | set(SITE_DOMAINS))

# Who may use the frozen web (archive pages, search, API). Comma-separated CIDRs.
# Empty = everyone who can reach the box (rely on the cloud firewall instead).
# Everyone else only sees the public pages: /, /healthz, /ca.pem, /join.sh and ACME challenges.
ALLOW = _list("FROZEN_ALLOW")

# Per-client-IP request limits (token bucket): sustained requests/second and burst size.
# Cache hits are cheap; these mostly stop one client from hammering the archives.
HTTP_RATE = float(os.environ.get("FROZEN_HTTP_RATE", "10"))
HTTP_BURST = int(os.environ.get("FROZEN_HTTP_BURST", "120"))
DNS_RATE = float(os.environ.get("FROZEN_DNS_RATE", "50"))
DNS_BURST = int(os.environ.get("FROZEN_DNS_BURST", "200"))

# Let's Encrypt HTTP-01 webroot and the issued cert (key + fullchain in one PEM).
ACME_DIR = DATA_DIR / "acme" / ".well-known" / "acme-challenge"
SITE_CERT = DATA_DIR / "tls" / "site.pem"

# Hostnames whose /search (or /) with ?q= is answered from the local search index.
SEARCH_HOSTS = {
    "www.google.com", "google.com", "www.bing.com", "bing.com",
    "duckduckgo.com", "html.duckduckgo.com", "search.frozen.internal",
}

# Timeout for upstream archive fetches (seconds).
FETCH_TIMEOUT = float(os.environ.get("FROZEN_FETCH_TIMEOUT", "25"))

# Max response body we store per page (bytes).
MAX_BODY = int(os.environ.get("FROZEN_MAX_BODY", str(15 * 1024 * 1024)))

CA_CERT_PATH = DATA_DIR / "ca" / "mitmproxy-ca-cert.pem"


def valid_snapshot(s: str) -> bool:
    return len(s) == 8 and s.isdigit() and "19960101" <= s <= "29991231"
