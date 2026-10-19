#!/bin/sh
# Frozen Web self-test. Run as root inside a sandbox (VM/container), NOT on your laptop:
#   curl -fsSL https://__FROZEN_DOMAIN__/selftest.sh | sudo sh
# Joins with --strict, checks what an unprivileged agent can and can't reach, prints a report,
# then undoes everything (set KEEP=1 to stay joined).
set -u
BASE="${FROZEN_BASE:-https://__FROZEN_DOMAIN__}"
MODE="${MODE:---strict}"
PASS=0; FAIL=0; WARN=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $*"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $*"; }
warn() { WARN=$((WARN+1)); echo "  WARN  $*"; }

[ "$(id -u)" = 0 ] || { echo "run as root: curl -fsSL $BASE/selftest.sh | sudo sh"; exit 1; }
echo "== Frozen Web self-test ($MODE) on $(uname -srm)"

# ---- tools -----------------------------------------------------------------
need=""
command -v curl >/dev/null 2>&1 || need="$need curl"
command -v iptables >/dev/null 2>&1 || need="$need iptables"
if [ -n "$need" ]; then
  echo "-- installing:$need"
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need ca-certificates >/dev/null 2>&1
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q $(echo "$need" | sed 's/iptables/iptables-nft/') ca-certificates >/dev/null 2>&1
  elif command -v yum >/dev/null 2>&1; then yum install -y -q $need ca-certificates >/dev/null 2>&1
  elif command -v apk >/dev/null 2>&1; then apk add -q $need ca-certificates >/dev/null 2>&1
  fi
fi
command -v curl >/dev/null 2>&1 || { echo "  FAIL  curl is missing and couldn't be installed"; exit 1; }

# The agent's own account (the user who ran sudo), and whether it can become root.
AGENT_USER="${SUDO_USER:-}"
if [ -n "$AGENT_USER" ] && [ "$AGENT_USER" != root ]; then
  if su -s /bin/sh "$AGENT_USER" -c 'sudo -n true' >/dev/null 2>&1; then
    warn "default user '$AGENT_USER' has passwordless sudo: an agent running as it can undo the lockdown. Run the agent as a user without sudo."
  else
    ok "default user '$AGENT_USER' has no passwordless sudo"
  fi
fi

# Which outside IPs answer before joining (so "blocked" afterwards means something).
TARGETS='https://1.1.1.1/ https://8.8.8.8/resolve?name=example.com https://9.9.9.9/ https://[2606:4700:4700::1111]/'
BEFORE=""
for t in $TARGETS; do curl -sg -m 5 -o /dev/null "$t" 2>/dev/null && BEFORE="$BEFORE $t"; done

# ---- join ------------------------------------------------------------------
echo "-- joining with $MODE"
curl -fsSL "$BASE/join.sh" >/tmp/frozen-join.sh 2>/tmp/frozen-join.log || { echo "  FAIL  can't download $BASE/join.sh"; exit 1; }
if sh /tmp/frozen-join.sh $MODE >/tmp/frozen-join.log 2>&1; then
  ok "join.sh $MODE completed"
else
  bad "join.sh failed:"; sed 's/^/        /' /tmp/frozen-join.log | tail -8
fi
grep -q "egress NOT locked" /tmp/frozen-join.log && bad "firewall not applied (no iptables / no NET_ADMIN)"

# Run as an unprivileged user, like an agent should.
as_agent() {
  if command -v setpriv >/dev/null 2>&1; then
    setpriv --reuid=65534 --regid=65534 --clear-groups sh -c "$1"
  else
    su -s /bin/sh nobody -c "$1"
  fi
}

echo "-- what an unprivileged agent sees"
h="$(as_agent 'curl -sS -m 30 -D - -o /dev/null https://example.com/' 2>&1)"
if echo "$h" | grep -qi '^x-frozen-source:'; then
  ok "https://example.com/ is served from the archive ($(echo "$h" | grep -i '^x-frozen-captured:' | tr -d '\r' | cut -d' ' -f2))"
else
  bad "https://example.com/ did not come from the frozen web: $(echo "$h" | head -2 | tr '\r\n' '  ')"
fi
w="$(as_agent 'curl -sS -m 15 https://frozen.internal/api/whoami' 2>&1)"
echo "$w" | grep -q '"snapshot"' && ok "frozen.internal answers: $(echo "$w" | tr -d ' \n')" || bad "frozen.internal unreachable: $w"

for target in $TARGETS; do
  case " $BEFORE " in
    *" $target "*) ;;
    *) case "$target" in *\[*) ;; *) warn "can't check $target: it wasn't reachable even before joining" ;; esac
       continue ;;
  esac
  if as_agent "curl -sg -m 5 -o /dev/null '$target'" >/dev/null 2>&1; then
    bad "real internet reachable by IP: $target"
  else
    ok "blocked: $target (hard-coded IP / DNS-over-HTTPS)"
  fi
done

if command -v iptables >/dev/null 2>&1 && iptables -S FROZEN >/dev/null 2>&1; then
  if [ "$MODE" = "--strict" ]; then
    iptables -S FROZEN | grep -qE '10\.0\.0\.0/8|172\.16\.0\.0/12|192\.168\.0\.0/16' \
      && bad "local networks are still allowed" || ok "local network (10/8, 172.16/12, 192.168/16) blocked"
  fi
  as_agent 'iptables -F FROZEN' >/dev/null 2>&1 && bad "unprivileged agent could remove the firewall" \
    || ok "unprivileged agent can't remove the firewall"
fi
as_agent 'echo nameserver 1.1.1.1 > /etc/resolv.conf' >/dev/null 2>&1 \
  && bad "unprivileged agent could change DNS" || ok "unprivileged agent can't change DNS"

# ---- cleanup ---------------------------------------------------------------
if [ "${KEEP:-0}" = 1 ]; then
  echo "-- KEEP=1: staying joined"
else
  sh /tmp/frozen-join.sh --leave >/dev/null 2>&1 && echo "-- left the frozen web (DNS + firewall restored)"
fi
echo "== $PASS passed, $FAIL failed, $WARN warnings"
[ "$FAIL" = 0 ]
