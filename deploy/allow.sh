#!/usr/bin/env bash
# Set which source IPs/CIDRs may use the frozen web DNS + gateway.
#   ./deploy/allow.sh 1.2.3.4/32,10.20.0.0/16
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: $0 <cidr[,cidr...]>"; exit 1; }
NAME="${NAME:-frozen-web}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
gcloud --project "$PROJECT" compute firewall-rules update "$NAME-allow" --source-ranges "$1"
echo "allowed: $1"
