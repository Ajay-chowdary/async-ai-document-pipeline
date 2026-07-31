#!/bin/bash
# Bootstrap a single Amazon Linux 2023 host with Docker Compose and the app.
# OPENAI_API_KEY is read from SSM at boot — it is never baked into the AMI or
# written into Terraform state by this module.
set -euo pipefail

exec > >(tee /var/log/docpipeline-user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

PROJECT_NAME="${project_name}"
AWS_REGION="${aws_region}"
OPENAI_SSM_PARAMETER_NAME="${openai_ssm_parameter_name}"
GIT_REPO_URL="${git_repo_url}"
GIT_REF="${git_ref}"
LLM_PROVIDER="${llm_provider}"
APP_DIR="/opt/$${PROJECT_NAME}"

echo "docpipeline bootstrap starting"

dnf update -y
dnf install -y docker git awscli curl

systemctl enable --now docker
usermod -aG docker ec2-user

COMPOSE_VERSION="v2.32.4"
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/download/$${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version

echo "fetching OPENAI_API_KEY from SSM parameter $${OPENAI_SSM_PARAMETER_NAME}"
OPENAI_API_KEY="$(aws ssm get-parameter \
  --name "$${OPENAI_SSM_PARAMETER_NAME}" \
  --with-decryption \
  --region "$${AWS_REGION}" \
  --query Parameter.Value \
  --output text)"

if [[ -z "$${OPENAI_API_KEY}" || "$${OPENAI_API_KEY}" == "None" ]]; then
  echo "OPENAI_API_KEY from SSM was empty" >&2
  exit 1
fi

rm -rf "$${APP_DIR}"
mkdir -p "$${APP_DIR}"
git clone --depth 1 --branch "$${GIT_REF}" "$${GIT_REPO_URL}" "$${APP_DIR}"
cd "$${APP_DIR}"

# Compose sets DATABASE_URL / REDIS_URL to the service hostnames in compose.yml.
# The key is written only onto this instance filesystem, mode 600.
umask 077
cat > "$${APP_DIR}/.env" <<EOF
ENVIRONMENT=production
LOG_JSON=true
LLM_PROVIDER=$${LLM_PROVIDER}
OPENAI_API_KEY=$${OPENAI_API_KEY}
OPENAI_MODEL=gpt-4o-mini
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=docpipeline
EOF
chmod 600 "$${APP_DIR}/.env"
chown -R ec2-user:ec2-user "$${APP_DIR}"

docker compose up -d --build

IMDS_TOKEN="$(curl -fsSX PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")"
PUBLIC_IP="$(curl -fsS -H "X-aws-ec2-metadata-token: $${IMDS_TOKEN}" \
  http://169.254.169.254/latest/meta-data/public-ipv4 || true)"

echo "docpipeline bootstrap finished"
echo "dashboard: http://$${PUBLIC_IP}:8000/"
