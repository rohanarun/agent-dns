#!/bin/bash
# Runs on the VM (as root) after the code is copied to /opt/frozen-web.
set -euo pipefail
cd /opt/frozen-web

# Docker may still be installing from the startup script on first boot.
for i in $(seq 1 60); do command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && break; sleep 5; done
command -v docker >/dev/null 2>&1 || bash deploy/vm-bootstrap.sh

# Bind DNS to the internal IP so it never collides with a local stub resolver on 127.0.0.53.
INTERNAL_IP="$(hostname -I | awk '{print $1}')"
grep -q '^FROZEN_DNS_BIND=' .env && sed -i "s/^FROZEN_DNS_BIND=.*/FROZEN_DNS_BIND=$INTERNAL_IP/" .env \
  || echo "FROZEN_DNS_BIND=$INTERNAL_IP" >> .env
chmod 600 .env

if ss -lunp | grep -q "$INTERNAL_IP:53 "; then
  echo "something already listens on $INTERNAL_IP:53"; ss -lunp | grep ':53 '; exit 1
fi

mkdir -p /var/lib/frozen-web
docker compose build --pull
docker compose up -d

for i in $(seq 1 30); do [ -f /var/lib/frozen-web/ca/mitmproxy-ca-cert.pem ] && break; sleep 2; done
docker compose ps
echo "CA: /var/lib/frozen-web/ca/mitmproxy-ca-cert.pem (private key alongside — never copy it off this VM)"
