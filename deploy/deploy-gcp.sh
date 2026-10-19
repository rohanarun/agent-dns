#!/usr/bin/env bash
# Deploy the frozen web to Google Cloud: one VM, one static IP that is both the DNS server
# and the gateway. Run from the repo root, logged in with `gcloud auth login`.
#
#   ./deploy/deploy-gcp.sh
#   PROJECT=my-proj SNAPSHOT=20250101 ALLOW="1.2.3.4/32,5.6.7.0/24" ./deploy/deploy-gcp.sh
#
# Re-running is safe: existing resources are reused and the code is redeployed.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-east4}"           # Virginia: low latency to Common Crawl's AWS us-east-1
ZONE="${ZONE:-${REGION}-a}"
NAME="${NAME:-frozen-web}"
MACHINE="${MACHINE:-e2-standard-2}"
DISK_GB="${DISK_GB:-100}"
SNAPSHOT="${SNAPSHOT:-20260601}"
PASSTHROUGH="${PASSTHROUGH:-}"
IAP="${IAP:-0}"                         # IAP=1 if your project has no SSH firewall rule

[ -n "$PROJECT" ] || { echo "set PROJECT or run: gcloud config set project <id>"; exit 1; }
if [ -z "${ALLOW:-}" ]; then
  MYIP="$(curl -fsS https://api.ipify.org)"
  ALLOW="${MYIP}/32"
  echo "ALLOW not set; only your current IP ($ALLOW) can use the DNS/gateway. Add more later with deploy/allow.sh"
fi

G="gcloud --project $PROJECT --quiet"
SSH_FLAGS=(--zone "$ZONE")
[ "$IAP" = 1 ] && SSH_FLAGS+=(--tunnel-through-iap)

echo "==> enabling Compute Engine API"
$G services enable compute.googleapis.com

echo "==> static IP"
$G compute addresses describe "$NAME" --region "$REGION" >/dev/null 2>&1 \
  || $G compute addresses create "$NAME" --region "$REGION"
IP="$($G compute addresses describe "$NAME" --region "$REGION" --format='value(address)')"
echo "    $IP"

echo "==> firewall: DNS + HTTP(S) from $ALLOW"
if $G compute firewall-rules describe "$NAME-allow" >/dev/null 2>&1; then
  $G compute firewall-rules update "$NAME-allow" --source-ranges "$ALLOW"
else
  $G compute firewall-rules create "$NAME-allow" --network default --direction INGRESS \
    --allow udp:53,tcp:53,tcp:80,tcp:443 --source-ranges "$ALLOW" --target-tags "$NAME"
fi
if [ "$IAP" = 1 ] && ! $G compute firewall-rules describe "$NAME-iap-ssh" >/dev/null 2>&1; then
  $G compute firewall-rules create "$NAME-iap-ssh" --network default --direction INGRESS \
    --allow tcp:22 --source-ranges 35.235.240.0/20 --target-tags "$NAME"
fi

echo "==> VM"
if ! $G compute instances describe "$NAME" --zone "$ZONE" >/dev/null 2>&1; then
  $G compute instances create "$NAME" --zone "$ZONE" --machine-type "$MACHINE" \
    --image-family debian-12 --image-project debian-cloud \
    --boot-disk-size "${DISK_GB}GB" --boot-disk-type pd-balanced \
    --address "$IP" --tags "$NAME" \
    --metadata-from-file startup-script=deploy/vm-bootstrap.sh
fi

echo "==> waiting for SSH"
for i in $(seq 1 40); do
  $G compute ssh "$NAME" "${SSH_FLAGS[@]}" --command "true" >/dev/null 2>&1 && break
  sleep 6
done

echo "==> config"
STATE_DIR="${HOME}/.frozen-web"; mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
TOKEN_FILE="$STATE_DIR/${NAME}-admin-token"
[ -f "$TOKEN_FILE" ] || { openssl rand -hex 24 > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"; }
STAGE="$(mktemp -d)"
cp -r gateway docker-compose.yml deploy "$STAGE/"
rm -rf "$STAGE/gateway/tests" "$STAGE/gateway/__pycache__" "$STAGE/gateway/frozen/__pycache__"
cat > "$STAGE/.env" <<EOF
FROZEN_PUBLIC_IP=$IP
FROZEN_SNAPSHOT=$SNAPSHOT
FROZEN_ADMIN_TOKEN=$(cat "$TOKEN_FILE")
FROZEN_PASSTHROUGH=$PASSTHROUGH
FROZEN_SOURCES=commoncrawl,wayback
EOF
tar -C "$STAGE" -czf "$STAGE.tgz" .
rm -rf "$STAGE"

echo "==> uploading + starting"
$G compute scp "${SSH_FLAGS[@]}" "$STAGE.tgz" "$NAME:/tmp/frozen-web.tgz"
rm -f "$STAGE.tgz"
$G compute ssh "$NAME" "${SSH_FLAGS[@]}" --command "sudo bash -c 'mkdir -p /opt/frozen-web && tar -C /opt/frozen-web -xzf /tmp/frozen-web.tgz && rm /tmp/frozen-web.tgz && bash /opt/frozen-web/deploy/vm-install.sh'"

echo "==> checking from here"
ok=0
for i in $(seq 1 20); do
  if curl -fsS "http://$IP/healthz" >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
[ "$ok" = 1 ] || echo "    gateway not answering yet; check: gcloud compute ssh $NAME --zone $ZONE --command 'cd /opt/frozen-web && sudo docker compose logs --tail 50'"
if command -v dig >/dev/null 2>&1; then
  echo "    dig google.com @$IP -> $(dig +short google.com @"$IP" | head -1)"
fi

cat <<EOF

────────────────────────────────────────────────────────────
 Frozen web is up.  Snapshot: $SNAPSHOT

 DNS / gateway IP:   $IP
 Status page:        http://$IP/
 Admin token:        $TOKEN_FILE

 Join an agent box (Linux, as root):
   curl -fsSL http://$IP/join.sh | sh -s -- --lockdown

 Docker agents:
   docker run --dns $IP ...   (and trust http://$IP/ca.pem; see agent-kit/)

 Seed search:
   gcloud compute ssh $NAME --zone $ZONE --command \\
     "cd /opt/frozen-web && sudo docker compose exec gateway python -m frozen.ingest --domain en.wikipedia.org --limit 2000"
────────────────────────────────────────────────────────────
EOF
