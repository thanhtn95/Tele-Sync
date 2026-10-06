#!/usr/bin/env bash
# Create the Always-Free e2-micro VM. Run from your machine with gcloud configured.
# Always Free requires: e2-micro, region us-central1 / us-west1 / us-east1,
# <= 30 GB *standard* persistent disk. No static IP (ephemeral IPs are fine: we use Tailscale).
set -euo pipefail

PROJECT=${PROJECT:?set PROJECT=<gcp-project-id>}
ZONE=${ZONE:-us-central1-a}
NAME=${NAME:-telesync}

gcloud compute instances create "$NAME" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --machine-type=e2-micro \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-standard \
  --scopes=storage-rw \
  --metadata=enable-oslogin=TRUE

# No inbound HTTP/HTTPS rules are created: the app is reachable only via Tailscale.
# The default network ships an RDP rule we don't need:
gcloud compute firewall-rules delete default-allow-rdp --project="$PROJECT" --quiet || true

cat <<MSG

VM created. Next:
  gcloud compute ssh $NAME --zone $ZONE --project $PROJECT
  git clone <your repo> /tmp/tele-sync && sudo bash /tmp/tele-sync/deploy/setup.sh

Optional, once Tailscale SSH works, close public SSH too:
  gcloud compute firewall-rules delete default-allow-ssh --project=$PROJECT

Budget alert (billing account id from 'gcloud billing accounts list'):
  gcloud billing budgets create --billing-account=<ACCOUNT_ID> \\
    --display-name="telesync" --budget-amount=1USD \\
    --threshold-rule=percent=0.5 --threshold-rule=percent=1.0
Backups bucket (US region keeps it inside the 5 GB free tier):
  gcloud storage buckets create gs://<bucket> --project=$PROJECT --location=us-central1
MSG
