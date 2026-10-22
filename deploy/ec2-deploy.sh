#!/bin/bash
# Build and (re)start Edit OS on one EC2 instance (Amazon Linux 2023). Run as root from the
# unpacked release folder; tools/deploy_aws.py uploads the release and runs this (user data on
# first boot, SSM Run Command after). Settings come from Secrets Manager (secret $SECRET_ID,
# a JSON object of environment variables), never from this script.
set -euo pipefail
SECRET_ID=${SECRET_ID:-edit-os/env}
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  dnf install -y docker
  systemctl enable --now docker
fi

# Swap so the image builds and LanguageTool (2 GB heap) fit on a 4 GB instance.
if ! swapon --show | grep -q /swapfile; then
  [ -f /swapfile ] || { dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none; chmod 600 /swapfile; mkswap /swapfile; }
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap defaults 0 0' >> /etc/fstab
fi

TOKEN=$(curl -sS -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
meta() { curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/$1"; }
REGION=$(meta placement/region)
PUBLIC_IP=$(meta public-ipv4 || true)

umask 077
aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" --query SecretString --output text \
  | python3 -c 'import json, sys; [print(f"{k}={v}") for k, v in json.load(sys.stdin).items()]' > /opt/edit-os.env
echo "AWS_REGION=$REGION" >> /opt/edit-os.env
grep -q '^EDITOS_PUBLIC_URL=' /opt/edit-os.env || echo "EDITOS_PUBLIC_URL=http://$PUBLIC_IP" >> /opt/edit-os.env

# Journal stylesheets and the pre-editing guide (client material, never in the release): synced from a private S3
# prefix when one is there -- EDITOS_STYLESHEETS_S3 in the secret, else s3://$EDITOS_S3_BUCKET/stylesheets/ --
# into /opt/edit-os-stylesheets and mounted read-only. Layout: <prefix>*.txt (one per journal) and
# <prefix>guide/*.txt (the guide). Without them the review screen shows each rule's plain description.
SHEETS_S3=$(sed -n 's/^EDITOS_STYLESHEETS_S3=//p' /opt/edit-os.env | tail -1)
BUCKET=$(sed -n 's/^EDITOS_S3_BUCKET=//p' /opt/edit-os.env | tail -1)
[ -z "$SHEETS_S3" ] && [ -n "$BUCKET" ] && SHEETS_S3="s3://$BUCKET/stylesheets/"
SHEETS_DIR=/opt/edit-os-stylesheets
SHEETS_ARGS=()
if [ -n "$SHEETS_S3" ] && aws s3 ls "$SHEETS_S3" --region "$REGION" >/dev/null 2>&1; then
  mkdir -p "$SHEETS_DIR" && chmod 700 "$SHEETS_DIR"
  aws s3 sync "$SHEETS_S3" "$SHEETS_DIR" --region "$REGION" --delete --only-show-errors
  SHEETS_ARGS=(-v "$SHEETS_DIR:/stylesheets:ro" -e EDITOS_STYLESHEETS=/stylesheets)
  GUIDE=$(ls "$SHEETS_DIR"/guide/*.txt 2>/dev/null | head -1 || true)
  [ -n "$GUIDE" ] && SHEETS_ARGS+=(-e "EDITOS_PREEDIT_GUIDE=/stylesheets/guide/$(basename "$GUIDE")")
  echo "stylesheets: $(ls "$SHEETS_DIR"/*.txt 2>/dev/null | wc -l) from $SHEETS_S3${GUIDE:+ (+ guide)}"
else
  echo "stylesheets: none at ${SHEETS_S3:-<no prefix>}; rule quotes off"
fi

docker network inspect editos >/dev/null 2>&1 || docker network create editos
docker build -q -t editos-languagetool -f deploy/languagetool.Dockerfile deploy
docker build -q -t editos .

docker rm -f editos-lt editos-api >/dev/null 2>&1 || true
docker run -d --name editos-lt --network editos --restart unless-stopped editos-languagetool
docker run -d --name editos-api --network editos --restart unless-stopped -p 80:8000 \
  --env-file /opt/edit-os.env -e EDITOS_LANGUAGETOOL_URL=http://editos-lt:8081 \
  -v editos-data:/data ${SHEETS_ARGS[@]+"${SHEETS_ARGS[@]}"} editos

for _ in $(seq 60); do
  curl -fsS http://127.0.0.1/api/health >/dev/null 2>&1 && break
  sleep 2
done
# NLM journal list for abbreviations (kept in the data volume; refreshed on every deploy).
docker exec editos-api mkdir -p /data/cache
docker exec editos-api python -m edit_os journals update || echo "warning: NLM journal list not updated"
docker image prune -f >/dev/null
echo "edit-os deployed: http://$PUBLIC_IP"
