#!/usr/bin/env bash
# Give the frozen web a real domain with a publicly trusted HTTPS certificate (Let's Encrypt).
# Run from the repo root on a machine with gcloud logged in to the project:
#
#   DOMAIN=tensor-book.com ./deploy/setup-https.sh
#
# What it does:
#   1. Opens tcp/80+443 to the internet (Let's Encrypt must reach port 80 to verify you own the
#      domain, and again every ~60 days to renew). DNS/53 stays restricted to your allowlist.
#   2. Moves the allowlist into the app (FROZEN_ALLOW): outsiders only see the public landing
#      page, /ca.pem and /join.sh; archive pages, search and the API stay allowlist-only.
#   3. Uploads the code (backing up the old copy), restarts, gets the cert with certbot, and
#      installs a renewal hook that reloads the gateway.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${DOMAIN:?set DOMAIN, e.g. DOMAIN=tensor-book.com}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
NAME="${NAME:-frozen-web}"
ZONE="${ZONE:-us-east4-a}"
REGION="${ZONE%-*}"
EMAIL="${EMAIL:-}"               # optional: Let's Encrypt expiry notices
WWW="${WWW:-1}"                   # also cover www.$DOMAIN
IAP="${IAP:-0}"
OPEN="${OPEN:-0}"                 # OPEN=1: anyone can use DNS + archive (rate-limited per IP)
G="gcloud --project $PROJECT --quiet"
SSH=(--zone "$ZONE"); [ "$IAP" = 1 ] && SSH+=(--tunnel-through-iap)

DOMAINS="$DOMAIN"; [ "$WWW" = 1 ] && DOMAINS="$DOMAIN,www.$DOMAIN"
IP="$($G compute addresses describe "$NAME" --region "$REGION" --format='value(address)')"
echo "==> $DOMAINS -> $IP"

echo "==> checking DNS"
for d in ${DOMAINS//,/ }; do
  got="$(dig +short "$d" A @1.1.1.1 | tail -1 || true)"
  if [ "$got" != "$IP" ]; then
    echo "    $d resolves to '${got:-nothing}', not $IP."
    echo "    Fix the A record (and www CNAME) at your registrar, wait a few minutes, then re-run."
    exit 1
  fi
  echo "    $d ok"
done

echo "==> firewall: 80/443 open to the internet"
# Use the VM's real network and tag (an earlier deploy may differ from this repo's defaults).
NET="$($G compute instances describe "$NAME" --zone "$ZONE" --format='value(networkInterfaces[0].network.basename())')"
TAG="$($G compute instances describe "$NAME" --zone "$ZONE" --format='value(tags.items[0])')"
if [ -z "$TAG" ]; then
  TAG="$NAME"
  $G compute instances add-tags "$NAME" --zone "$ZONE" --tags "$TAG"
fi
echo "    network=$NET tag=$TAG"
if [ "$OPEN" = 1 ]; then
  ALLOW=""
  # DNS open to everyone. SSH and everything else stay closed by the deny rule.
  if $G compute firewall-rules describe "$NAME-dns-public" >/dev/null 2>&1; then
    $G compute firewall-rules update "$NAME-dns-public" --allow udp:53,tcp:53 --source-ranges 0.0.0.0/0 \
      --target-tags "$TAG" --priority 850
  else
    $G compute firewall-rules create "$NAME-dns-public" --network "$NET" --direction INGRESS --priority 850 \
      --allow udp:53,tcp:53 --source-ranges 0.0.0.0/0 --target-tags "$TAG"
  fi
else
  ALLOW="${ALLOW:-$($G compute firewall-rules describe "$NAME-allow" --format='value(sourceRanges.list())' 2>/dev/null || true)}"
  [ -n "$ALLOW" ] || { echo "    couldn't find the existing allowlist rule; set ALLOW=1.2.3.4/32 and re-run"; exit 1; }
  $G compute firewall-rules delete "$NAME-dns-public" >/dev/null 2>&1 || true
fi
if $G compute firewall-rules describe "$NAME-web" >/dev/null 2>&1; then
  $G compute firewall-rules update "$NAME-web" --allow tcp:80,tcp:443 --source-ranges 0.0.0.0/0 \
    --target-tags "$TAG" --priority 850
else
  $G compute firewall-rules create "$NAME-web" --network "$NET" --direction INGRESS --priority 850 \
    --allow tcp:80,tcp:443 --source-ranges 0.0.0.0/0 --target-tags "$TAG"
fi
echo "    app-level allowlist: ${ALLOW:-none (open to everyone, rate-limited per IP)}"

echo "==> uploading code (old copy kept as /opt/frozen-web.bak-<time>)"
STAGE="$(mktemp -d)"
cp -r gateway docker-compose.yml deploy "$STAGE/"
rm -rf "$STAGE/gateway/tests"; find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
tar -C "$STAGE" -czf "$STAGE.tgz" . && rm -rf "$STAGE"
$G compute scp "${SSH[@]}" "$STAGE.tgz" "$NAME:/tmp/frozen-web.tgz"; rm -f "$STAGE.tgz"

$G compute ssh "$NAME" "${SSH[@]}" --command "sudo DOMAINS='$DOMAINS' ALLOW='$ALLOW' EMAIL='$EMAIL' bash -s" <<'REMOTE'
set -euo pipefail
cd /opt
cp -a frozen-web "frozen-web.bak-$(date +%Y%m%d%H%M%S)"
tar -C /opt/frozen-web -xzf /tmp/frozen-web.tgz && rm /tmp/frozen-web.tgz
cd /opt/frozen-web
setenv() { grep -q "^$1=" .env && sed -i "s|^$1=.*|$1=$2|" .env || echo "$1=$2" >> .env; }
setenv FROZEN_SITE_DOMAINS "$DOMAINS"
setenv FROZEN_ALLOW "$ALLOW"
chmod 600 .env
mkdir -p /var/lib/frozen-web/acme/.well-known/acme-challenge /var/lib/frozen-web/tls
docker compose build
docker compose up -d

# Wait until the gateway answers on port 80.
for i in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1/healthz && break; sleep 2; done

command -v certbot >/dev/null || { apt-get update -y && apt-get install -y certbot; }

# Renewal hook: bundle key + chain for mitmproxy, then restart the gateway to load it.
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/frozen-web.sh <<'HOOK'
#!/bin/sh
set -e
D="$RENEWED_LINEAGE"
umask 077
cat "$D/privkey.pem" "$D/fullchain.pem" > /var/lib/frozen-web/tls/site.pem.new
mv /var/lib/frozen-web/tls/site.pem.new /var/lib/frozen-web/tls/site.pem
cd /opt/frozen-web && docker compose restart gateway
HOOK
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/frozen-web.sh

ARGS=(certonly --webroot -w /var/lib/frozen-web/acme --non-interactive --agree-tos
      --cert-name frozen-web --keep-until-expiring)
for d in ${DOMAINS//,/ }; do ARGS+=(-d "$d"); done
if [ -n "$EMAIL" ]; then ARGS+=(-m "$EMAIL"); else ARGS+=(--register-unsafely-without-email); fi
certbot "${ARGS[@]}"

# First issuance: run the hook ourselves (certbot only runs deploy hooks on new certs).
RENEWED_LINEAGE=/etc/letsencrypt/live/frozen-web sh /etc/letsencrypt/renewal-hooks/deploy/frozen-web.sh
systemctl enable --now certbot.timer 2>/dev/null || true
sleep 4
docker compose logs --tail 5 gateway
REMOTE

echo "==> checking https://$DOMAIN"
curl -fsS "https://$DOMAIN/healthz" && echo "  <- valid public certificate, all good"
echo
echo "Landing page:  https://$DOMAIN/"
echo "Agents still use $IP as their DNS server; join with:"
echo "  curl -fsSL https://$DOMAIN/join.sh | sudo sh -s -- --lockdown"
