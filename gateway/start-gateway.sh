#!/bin/sh
# Starts mitmdump. If a real (Let's Encrypt) cert exists, it is served for the public site
# domains; every other hostname still gets a cert minted by the frozen-web CA.
set -eu
CERT=/data/tls/site.pem
set -- mitmdump -q \
  --set confdir=/data/ca \
  --mode reverse:http://127.0.0.1:9@443 \
  --mode reverse:http://127.0.0.1:9@80 \
  --set connection_strategy=lazy \
  --set keep_host_header=true \
  --set block_global=false \
  --set upstream_cert=false
if [ -s "$CERT" ] && [ -n "${FROZEN_SITE_DOMAINS:-}" ]; then
  for d in $(echo "$FROZEN_SITE_DOMAINS" | tr ',' ' '); do
    set -- "$@" --certs "$d=$CERT"
  done
  echo "serving public cert for: $FROZEN_SITE_DOMAINS"
fi
exec "$@" -s /app/frozen_addon.py
