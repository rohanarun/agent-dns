#!/bin/sh
# Join this machine/container to the frozen web.
#   curl -fsSL http://<FROZEN_IP>/join.sh | sudo sh                 # DNS + CA
#   curl -fsSL http://<FROZEN_IP>/join.sh | sudo sh -s -- --lockdown  # + block all other egress
#   curl -fsSL http://<FROZEN_IP>/join.sh | sudo sh -s -- --strict    # + block the local network too
#   sudo sh join.sh --leave                                          # undo DNS + lockdown
set -eu

FROZEN_IP="${FROZEN_IP:-__FROZEN_IP__}"
FROZEN_DOMAIN="${FROZEN_DOMAIN:-__FROZEN_DOMAIN__}"
case "$FROZEN_DOMAIN" in __*) FROZEN_DOMAIN="" ;; esac
LOCKDOWN=0
STRICT=0
LEAVE=0
for a in "$@"; do
  case "$a" in
    --lockdown) LOCKDOWN=1 ;;
    --strict) LOCKDOWN=1; STRICT=1 ;;
    --leave) LEAVE=1 ;;
    *) FROZEN_IP="$a" ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
OS="$(uname -s)"

if [ "$LEAVE" = 1 ]; then
  if [ "$OS" = Darwin ]; then
    networksetup -listallnetworkservices | tail -n +2 | while read -r s; do networksetup -setdnsservers "$s" empty 2>/dev/null || true; done
  else
    if [ -f /run/frozen-resolv.conf ] && grep -q " /etc/resolv.conf " /proc/mounts 2>/dev/null; then
      umount /etc/resolv.conf 2>/dev/null || true
      rm -f /run/frozen-resolv.conf
    fi
    [ -f /etc/resolv.conf.frozen-bak ] && cp /etc/resolv.conf.frozen-bak /etc/resolv.conf 2>/dev/null || true
    if command -v iptables >/dev/null 2>&1; then
      iptables -D OUTPUT -j FROZEN 2>/dev/null || true; iptables -F FROZEN 2>/dev/null || true; iptables -X FROZEN 2>/dev/null || true
      ip6tables -D OUTPUT -j FROZEN6 2>/dev/null || true; ip6tables -F FROZEN6 2>/dev/null || true; ip6tables -X FROZEN6 2>/dev/null || true
      [ "$(sysctl -n net.ipv6.conf.all.disable_ipv6 2>/dev/null)" = 1 ] && [ -f /run/frozen-ipv6-off ] && \
        { sysctl -qw net.ipv6.conf.all.disable_ipv6=0 net.ipv6.conf.default.disable_ipv6=0 2>/dev/null; rm -f /run/frozen-ipv6-off; }
    fi
    rm -f /etc/profile.d/frozen-web.sh
  fi
  echo "left the frozen web (CA left installed; remove it manually if you want)"
  exit 0
fi

fetch() { if command -v curl >/dev/null 2>&1; then curl -fsSL "$1"; else wget -qO- "$1"; fi; }

# Prefer HTTPS on the public domain (real certificate, so the CA download can't be tampered with).
if [ -n "$FROZEN_DOMAIN" ]; then CA_URL="https://$FROZEN_DOMAIN/ca.pem"; else CA_URL="http://$FROZEN_IP/ca.pem"; fi
echo "→ downloading CA from $CA_URL"
TMP_CA="$(mktemp)"
fetch "$CA_URL" > "$TMP_CA"
grep -q "BEGIN CERTIFICATE" "$TMP_CA" || { echo "could not fetch CA"; exit 1; }

echo "→ trusting CA"
if [ "$OS" = Darwin ]; then
  security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain "$TMP_CA"
  CA_FILE="/usr/local/share/frozen-web-ca.pem"; mkdir -p /usr/local/share; cp "$TMP_CA" "$CA_FILE"
elif [ -d /usr/local/share/ca-certificates ] || command -v update-ca-certificates >/dev/null 2>&1; then
  mkdir -p /usr/local/share/ca-certificates
  cp "$TMP_CA" /usr/local/share/ca-certificates/frozen-web.crt
  update-ca-certificates >/dev/null 2>&1 || true
  CA_FILE=/usr/local/share/ca-certificates/frozen-web.crt
elif [ -d /etc/pki/ca-trust/source/anchors ]; then
  cp "$TMP_CA" /etc/pki/ca-trust/source/anchors/frozen-web.pem
  update-ca-trust
  CA_FILE=/etc/pki/ca-trust/source/anchors/frozen-web.pem
else
  mkdir -p /etc/ssl/certs; cp "$TMP_CA" /etc/ssl/certs/frozen-web.pem; CA_FILE=/etc/ssl/certs/frozen-web.pem
  echo "  (unknown distro: CA copied to $CA_FILE; point your tools at it)"
fi

BUNDLE=""
for b in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
  [ -f "$b" ] && { BUNDLE="$b"; break; }
done

# Chrome/Chromium on Linux read the per-user NSS database, not the system store.
if command -v certutil >/dev/null 2>&1; then
  for home in /root /home/*; do
    [ -d "$home" ] || continue
    mkdir -p "$home/.pki/nssdb"
    # never let certutil prompt (it spins forever on an existing database)
    [ -f "$home/.pki/nssdb/cert9.db" ] || certutil -d "sql:$home/.pki/nssdb" -N --empty-password </dev/null >/dev/null 2>&1 || true
    certutil -d "sql:$home/.pki/nssdb" -A -t "C,," -n frozen-web -i "$TMP_CA" </dev/null >/dev/null 2>&1 || true
  done
fi

if [ "$OS" != Darwin ]; then
  cat > /etc/profile.d/frozen-web.sh <<EOF
export NODE_EXTRA_CA_CERTS="$CA_FILE"
${BUNDLE:+export REQUESTS_CA_BUNDLE="$BUNDLE"
export SSL_CERT_FILE="$BUNDLE"
export CURL_CA_BUNDLE="$BUNDLE"}
EOF
fi

echo "→ pointing DNS at $FROZEN_IP"
if [ "$OS" = Darwin ]; then
  networksetup -listallnetworkservices | tail -n +2 | while read -r s; do networksetup -setdnsservers "$s" "$FROZEN_IP" 2>/dev/null || true; done
  dscacheutil -flushcache; killall -HUP mDNSResponder 2>/dev/null || true
else
  RESOLV="$(printf 'nameserver %s\noptions single-request timeout:3 attempts:2\n' "$FROZEN_IP")"
  [ -f /etc/resolv.conf.frozen-bak ] || cp -L /etc/resolv.conf /etc/resolv.conf.frozen-bak 2>/dev/null || true
  # replace a systemd-resolved symlink with a static file
  [ -L /etc/resolv.conf ] && rm -f /etc/resolv.conf 2>/dev/null
  if ! { printf '%s\n' "$RESOLV" > /etc/resolv.conf; } 2>/dev/null; then
    # Some sandboxes (e.g. Vercel) mount /etc/resolv.conf read-only: mount ours over it.
    mkdir -p /run && printf '%s\n' "$RESOLV" > /run/frozen-resolv.conf
    if mount --bind /run/frozen-resolv.conf /etc/resolv.conf 2>/dev/null; then
      echo "  /etc/resolv.conf is read-only here; mounted the frozen web's over it"
    else
      echo "  can't change DNS: /etc/resolv.conf is read-only and mounting over it isn't allowed"
      exit 1
    fi
  fi
fi

if [ "$LOCKDOWN" = 1 ]; then
  if [ "$STRICT" = 1 ]; then
    echo "→ strict lockdown: only $FROZEN_IP and this machine are reachable (no local network)"
  else
    echo "→ locking down egress: only $FROZEN_IP and private networks are reachable (use --strict to block those too)"
  fi
  if [ "$OS" = Darwin ]; then
    echo "  lockdown isn't automated on macOS; use a Linux container/VM for sealed runs"
  elif command -v iptables >/dev/null 2>&1; then
    iptables -N FROZEN 2>/dev/null || iptables -F FROZEN
    iptables -A FROZEN -o lo -j RETURN
    if [ "$STRICT" = 1 ]; then
      # only replies to connections made INTO this machine (e.g. your harness); connections the
      # agent opened before the lockdown don't survive it
      iptables -A FROZEN -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -j RETURN 2>/dev/null || true
    else
      iptables -A FROZEN -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN 2>/dev/null || true
    fi
    iptables -A FROZEN -d "$FROZEN_IP" -j RETURN
    if [ "$STRICT" != 1 ]; then
      iptables -A FROZEN -d 10.0.0.0/8 -j RETURN
      iptables -A FROZEN -d 172.16.0.0/12 -j RETURN
      iptables -A FROZEN -d 192.168.0.0/16 -j RETURN
    fi
    iptables -A FROZEN -j REJECT
    iptables -C OUTPUT -j FROZEN 2>/dev/null || iptables -I OUTPUT 1 -j FROZEN
    # IPv6: block everything but loopback. Some kernels lack the IPv6 REJECT target (e.g. Vercel),
    # so fall back to DROP, and if ip6tables can't be used at all, switch IPv6 off.
    v6=0
    if command -v ip6tables >/dev/null 2>&1 && {
         { ip6tables -N FROZEN6 2>/dev/null || ip6tables -F FROZEN6; } &&
         ip6tables -A FROZEN6 -o lo -j RETURN &&
         { ip6tables -A FROZEN6 -j REJECT 2>/dev/null || ip6tables -A FROZEN6 -j DROP; } &&
         { ip6tables -C OUTPUT -j FROZEN6 2>/dev/null || ip6tables -I OUTPUT 1 -j FROZEN6; }
       } 2>/dev/null; then
      v6=1
    fi
    if [ "$v6" = 0 ]; then
      if sysctl -qw net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1 2>/dev/null; then
        touch /run/frozen-ipv6-off; echo "  ip6tables unusable here; turned IPv6 off instead"
      elif [ -n "$(ip -6 route show default 2>/dev/null)" ]; then
        echo "  WARNING: couldn't block IPv6 and this machine has an IPv6 route"; exit 1
      fi
    fi
  else
    echo "  iptables not found (containers need --cap-add NET_ADMIN); egress NOT locked"
  fi
fi

echo "→ checking"
if command -v curl >/dev/null 2>&1; then
  curl -fsS ${BUNDLE:+--cacert "$BUNDLE"} https://frozen.internal/api/whoami && echo
fi
echo "✓ joined. New shells pick up CA env vars from /etc/profile.d/frozen-web.sh"
