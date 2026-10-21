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

TOKEN=$(curl -sS -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
meta() { curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/$1"; }
REGION=$(meta placement/region)
PUBLIC_IP=$(meta public-ipv4 || true)

umask 077
aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" --query SecretString --output text \
  | python3 -c 'import json, sys; [print(f"{k}={v}") for k, v in json.load(sys.stdin).items()]' > /opt/edit-os.env
echo "AWS_REGION=$REGION" >> /opt/edit-os.env
grep -q '^EDITOS_PUBLIC_URL=' /opt/edit-os.env || echo "EDITOS_PUBLIC_URL=http://$PUBLIC_IP" >> /opt/edit-os.env

docker network inspect editos >/dev/null 2>&1 || docker network create editos
docker build -q -t editos-languagetool -f deploy/languagetool.Dockerfile deploy
docker build -q -t editos .

docker rm -f editos-lt editos-api >/dev/null 2>&1 || true
docker run -d --name editos-lt --network editos --restart unless-stopped editos-languagetool
docker run -d --name editos-api --network editos --restart unless-stopped -p 80:8000 \
  --env-file /opt/edit-os.env -e EDITOS_LANGUAGETOOL_URL=http://editos-lt:8081 \
  -v editos-data:/data editos

for _ in $(seq 60); do
  curl -fsS http://127.0.0.1/api/health >/dev/null 2>&1 && break
  sleep 2
done
# NLM journal list for abbreviations (kept in the data volume; refreshed on every deploy).
docker exec editos-api mkdir -p /data/cache
docker exec editos-api python -m edit_os journals update || echo "warning: NLM journal list not updated"
docker image prune -f >/dev/null
echo "edit-os deployed: http://$PUBLIC_IP"
