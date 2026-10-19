#!/bin/sh
set -eu
: "${FROZEN_IP:?set FROZEN_IP to the frozen web IP}"
curl -fsSL "http://$FROZEN_IP/join.sh" | sh -s -- "$FROZEN_IP" --strict
. /etc/profile.d/frozen-web.sh
export NODE_EXTRA_CA_CERTS REQUESTS_CA_BUNDLE SSL_CERT_FILE CURL_CA_BUNDLE
# give the agent user Chrome trust too (join.sh populated /home/agent/.pki if certutil ran)
chown -R agent:agent /home/agent 2>/dev/null || true
exec setpriv --reuid=agent --regid=agent --init-groups --inh-caps=-all --bounding-set=-all \
  env HOME=/home/agent "$@"
