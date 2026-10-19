#!/usr/bin/env bash
# Usage report for the frozen web: requests, unique users, top sites, by day.
#   PROJECT=your-gcp-project bash ./deploy/usage.sh
#   EXCLUDE="203.0.113.7" PROJECT=... bash ./deploy/usage.sh   # leave your own IPs out
set -euo pipefail
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
NAME="${NAME:-frozen-web}"
ZONE="${ZONE:-us-east4-a}"
EXCLUDE="${EXCLUDE:-}"
SSH=(--zone "$ZONE"); [ "${IAP:-0}" = 1 ] && SSH+=(--tunnel-through-iap)

gcloud --project "$PROJECT" compute ssh "$NAME" "${SSH[@]}" \
  --command "cd /opt/frozen-web && sudo docker compose exec -T -e EXCLUDE='$EXCLUDE' gateway python -" <<'PY'
import os, sqlite3, time
db = sqlite3.connect("/data/frozen.sqlite")
ex = {"127.0.0.1", "::1"} | {x.strip() for x in os.environ.get("EXCLUDE", "").split(",") if x.strip()}
marks = ",".join("?" * len(ex))
W = f"WHERE client NOT IN ({marks})"
q = lambda sql, *a: db.execute(sql, tuple(ex) + a).fetchall()
fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)) if t else "-"

n, users, first, last = q(f"SELECT count(*), count(DISTINCT client), min(ts), max(ts) FROM log {W}")[0]
print(f"\nFrozen web usage (UTC){'  excluding ' + ', '.join(sorted(ex - {'127.0.0.1','::1'})) if len(ex) > 2 else ''}")
print(f"  requests: {n:,}   unique client IPs: {users:,}   first: {fmt(first)}   last: {fmt(last)}")
print(f"  cached pages: {db.execute('SELECT count(*) FROM pages').fetchone()[0]:,}"
      f"   pinned snapshots: {db.execute('SELECT count(*) FROM clients').fetchone()[0]:,}")

print("\nBy day:")
for d, c, u in q(f"SELECT date(ts,'unixepoch'), count(*), count(DISTINCT client) FROM log {W} GROUP BY 1 ORDER BY 1"):
    print(f"  {d}  {c:>7,} requests  {u:>5,} IPs")

print("\nWhat was served:")
for s, c in q(f"SELECT source, count(*) FROM log {W} GROUP BY 1 ORDER BY 2 DESC"):
    print(f"  {s:<12} {c:>7,}")

print("\nTop clients:")
for ip, c, a, b in q(f"SELECT client, count(*), min(ts), max(ts) FROM log {W} GROUP BY 1 ORDER BY 2 DESC LIMIT 15"):
    print(f"  {ip:<40} {c:>6,}  {fmt(a)} -> {fmt(b)}")

print("\nTop sites (archive fetches):")
for h, c in q(f"""SELECT substr(url, instr(url,'://')+3, instr(substr(url, instr(url,'://')+3),'/')-1), count(*)
                 FROM log {W} AND source NOT IN ('control','denied','ratelimited') GROUP BY 1 ORDER BY 2 DESC LIMIT 15"""):
    print(f"  {h:<40} {c:>6,}")

print("\nAPI / site use:")
for p, c in q(f"""SELECT CASE WHEN url LIKE '%/api/fetch%' THEN '/api/fetch' WHEN url LIKE '%/api/search%' THEN '/api/search'
                         WHEN url LIKE '%/join.sh%' THEN '/join.sh' WHEN url LIKE '%/selftest.sh%' THEN '/selftest.sh'
                         WHEN url LIKE '%/api/snapshot%' THEN '/api/snapshot' ELSE 'other control page' END, count(*)
                 FROM log {W} AND source='control' GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  {p:<20} {c:>6,}")
print("\n(DNS lookups aren't logged; this counts HTTP(S) requests to the gateway.)")
PY
