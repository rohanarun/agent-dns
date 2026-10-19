# agent-dns

A frozen copy of the internet for AI agents. Point a sandbox's DNS at one IP and every website it visits is served from archived pages from before a chosen date, with nothing live to reach.

**Hosted instance:** [tensor-book.com](https://tensor-book.com): free, no sign-up, rate-limited per IP. Or run your own on one Google Cloud VM (below).

```bash
# inside a VM or container (not your laptop); then run your agent as a non-root user with no sudo
curl -fsSL https://tensor-book.com/join.sh | sudo sh -s -- --strict

# check what an agent can and can't reach (joins, tests, then undoes everything)
curl -fsSL https://tensor-book.com/selftest.sh | sudo sh

# or skip DNS and use the API
curl "https://tensor-book.com/api/fetch?url=https://arxiv.org/abs/1706.03762"
curl "https://tensor-book.com/api/search?q=attention"
```

Tested on Daytona and Vercel sandboxes.

Give an agent **one IP address** as its DNS server. Every hostname it looks up resolves back to that same IP, where a gateway answers every request:

- **Archived pages:** served from Common Crawl, falling back to the Wayback Machine. Each page is the newest capture taken **on or before the snapshot date**, so nothing newer can leak in.
- **Search engines:** `google.com/search`, `bing.com/search` and `duckduckgo.com` are answered from a local full-text index of the frozen corpus.
- **Everything else** gets a clean 404 ("Not in the frozen web"). Forms and other writes return 405.

The first fetch of each page is pinned in a cache. After that, every run against the same snapshot sees exactly the same web.

```
 agent box ──DNS──▶ 34.x.x.x:53   "every name → 34.x.x.x"
     │
     └──HTTPS (SNI: arxiv.org)──▶ 34.x.x.x:443  gateway
                                     ├─ cert minted on the fly for arxiv.org (signed by your CA)
                                     ├─ cache hit? serve it
                                     ├─ Common Crawl: newest capture ≤ snapshot
                                     ├─ Wayback: newest capture ≤ snapshot
                                     └─ else 404
```

## Deploy (about 5 minutes)

Prerequisites: `gcloud` installed and logged in (`gcloud auth login`), and a project with billing enabled.

```bash
gcloud config set project YOUR_PROJECT
./deploy/deploy-gcp.sh
```

Options are set as environment variables:

| var | default | |
|---|---|---|
| `SNAPSHOT` | `20260601` | default "web as of" date |
| `ALLOW` | your current IP | comma-separated CIDRs allowed to use DNS and the gateway |
| `REGION` / `ZONE` | `us-east4` / `us-east4-a` | Virginia, close to Common Crawl's S3 |
| `MACHINE` | `e2-standard-2` | |
| `DISK_GB` | `100` | holds the cache and the search index |
| `PASSTHROUGH` | empty | real-internet hosts, e.g. `pypi.org,files.pythonhosted.org` |
| `IAP` | `0` | set to `1` if your project blocks SSH from the internet |

Then give it a real domain with a Let's Encrypt certificate, and optionally open it to everyone (rate-limited per IP):

```bash
DOMAIN=example.com ./deploy/setup-https.sh          # point the domain's A record at the VM first
OPEN=1 DOMAIN=example.com ./deploy/setup-https.sh   # no allowlist; anyone can use it
./deploy/usage.sh                                   # requests, unique IPs, top sites, by day
```

The deploy script prints the IP and saves the admin token to `~/.frozen-web/frozen-web-admin-token`. Re-running it redeploys the code and reuses the existing IP and VM.

To change who can connect later: `./deploy/allow.sh 1.2.3.4/32,10.0.0.0/8`

**Why there's an allowlist:** an open DNS server on the internet gets abused, and the gateway serves archived content. Keep both limited to your agent machines.

## Connect an agent

A sealed agent needs three things:
1. **DNS** pointed at the frozen IP.
2. **The CA** trusted, so HTTPS verifies.
3. **Egress blocked** to everything except the frozen IP. DNS alone isn't isolation: hardcoded IPs and DNS-over-HTTPS would bypass it.
4. **A non-root agent with no sudo**, so it can't remove the firewall rules or change its DNS back.

`--strict` allows only the frozen web and localhost. `--lockdown` also allows private networks (10.x, 172.16–31.x, 192.168.x) for agents that need a local model server or database; it's weaker, because the agent can reach anything on that local network.

**Linux box or VM (as root):**
```bash
curl -fsSL http://FROZEN_IP/join.sh | sh -s -- --strict
# undo:
curl -fsSL http://FROZEN_IP/join.sh | sh -s -- --leave
```

**Docker:**
```bash
cd agent-kit && docker build -t frozen-agent .
docker run --rm -it --dns FROZEN_IP -e FROZEN_IP=FROZEN_IP --cap-add NET_ADMIN frozen-agent bash
# inside:  curl https://en.wikipedia.org/wiki/Integer_multiplication   → archived copy
#          curl https://example.org/ → archived; curl 1.1.1.1 → blocked
```
The entrypoint locks down egress as root, then drops to an unprivileged `agent` user so the agent can't undo it.

**Headless Chrome / Playwright** reads the NSS database. `join.sh` populates it when `certutil` (libnss3-tools) is installed. Otherwise launch with `--ignore-certificate-errors`, but only inside the sealed sandbox.

## Per-agent snapshots

Each agent's source IP can be pinned to its own date. For example, agent A sees the web as of 2025-01-01 while agent B sees 2026-06-01:

```bash
TOKEN=$(cat ~/.frozen-web/frozen-web-admin-token)
curl -X POST http://FROZEN_IP/admin/client -H "Authorization: Bearer $TOKEN" \
  -d '{"ip":"203.0.113.7","snapshot":"20250101","label":"run-42"}'
curl -X DELETE http://FROZEN_IP/admin/client -H "Authorization: Bearer $TOKEN" -d '{"ip":"203.0.113.7"}'
```

Agents can also move *themselves* to an earlier date, never later than the server's cutoff, with no token:
`curl -X POST https://FROZEN_HOST/api/snapshot -d '{"snapshot":"20250101"}'` (`"default"` resets it).

## Seed search

Search covers every page any agent has visited, plus whatever you seed:

```bash
gcloud compute ssh frozen-web --zone us-east4-a --command \
  "cd /opt/frozen-web && sudo docker compose exec gateway python -m frozen.ingest \
     --domain en.wikipedia.org --domain arxiv.org --limit 5000"
# or a URL list:  ... python -m frozen.ingest --urls /data/urls.txt
```

## API (for agents that don't browse)

All endpoints are on `http(s)://frozen.internal` or `http://FROZEN_IP`:

```
GET  /                         status page
GET  /ca.pem /join.sh /selftest.sh   bootstrap and self-test
GET  /api/whoami               your IP + snapshot
GET  /api/search?q=...&n=10    JSON search over the frozen corpus
GET  /api/fetch?url=...        JSON (title, text, captured date); &format=raw for original bytes
GET  /api/log?limit=200        your own request log (admin: &client=IP or client=*)
POST /api/snapshot             move yourself to an earlier date (no token)
POST /admin/client             set any IP's snapshot (Bearer token)
GET  /admin/clients
```

Every response from the frozen web carries `X-Frozen-Source`, `X-Frozen-Captured` and `X-Frozen-Snapshot` headers.

## Limits, honestly

- **Coverage is what Common Crawl and the Wayback Machine have.** Popular pages are usually there. Long-tail pages, JS-rendered apps and anything behind a login are not.
- **Frozen means read-only.** Logins, POST forms and APIs that need live servers don't work. For interactive tasks, run WebArena/WebHarbor-style clones and add their hostnames to the gateway.
- **The first fetch of a page is slow** (roughly 1–10 s), because it waits on the Common Crawl index or Wayback. Repeat fetches come from the local cache. Seed ahead of large runs.
- **Re-serving archived pages raises copyright questions.** Use it for research and evals.
- **DNS and the firewall aren't a sandbox.** `--strict` plus a non-root agent means the only network it can reach is the frozen web; a bug that breaks out of the container or VM itself still lands on the host. Platform-level egress limits (e.g. Vercel's `--allowed-cidr`) are stronger, because the agent can't change them even with sudo.
- **Protect the CA key.** It lives at `/var/lib/frozen-web/ca/mitmproxy-ca.pem` on the VM. Anyone holding it can impersonate any website to machines that trust it, so only install the CA in sandboxes.

## Cost (rough)

e2-standard-2 (about $50/mo), plus a static IP, a 100 GB balanced disk (about $10/mo), and small egress. Stop the VM when it's idle:
`gcloud compute instances stop frozen-web --zone us-east4-a`. The IP and cache are kept.

## Layout

```
gateway/frozen/      core.py (gateway logic), sources.py (Common Crawl + Wayback, date-capped),
                     store.py (cache, FTS search, logs, per-IP snapshots), dns_server.py, ingest.py
gateway/join.sh      agent bootstrap (served at /join.sh): --strict, --lockdown, --leave
gateway/selftest.sh  in-sandbox check of what an agent can reach (served at /selftest.sh)
gateway/tests/       unit + end-to-end tests (real mitmproxy, real TLS, real DNS)
deploy/              deploy-gcp.sh, setup-https.sh, usage.sh, allow.sh, vm-bootstrap.sh, vm-install.sh
agent-kit/           sealed agent container example
```

## Tests

```bash
pip install mitmproxy==12.2.3 dnslib warcio httpx beautifulsoup4 pytest
cd gateway && python3 -m pytest -q tests/   # firewall tests run when executed as root with iproute2 + iptables
```

## License

MIT
