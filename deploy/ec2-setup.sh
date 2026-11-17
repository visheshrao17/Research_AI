#!/usr/bin/env bash
# One-time EC2 bootstrap for ResearchX. Run on the instance as the deploy user (with sudo rights):
#   curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/deploy/ec2-setup.sh | bash
# or copy it over and `bash ec2-setup.sh`. Safe to re-run.
# Supports Ubuntu 22.04/24.04 and Amazon Linux 2023. Installs Docker Engine + the compose plugin,
# adds swap on small instances, and creates /opt/researchx for the deploy workflow.
set -euo pipefail
APP_DIR=/opt/researchx
DEPLOY_USER="${SUDO_USER:-$USER}"

if ! command -v docker >/dev/null 2>&1; then
  . /etc/os-release
  if [ "${ID}" = "amzn" ]; then
    sudo dnf install -y docker
    sudo mkdir -p /usr/local/lib/docker/cli-plugins
    arch=$(uname -m); [ "$arch" = "aarch64" ] || arch=x86_64
    sudo curl -fsSL "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-${arch}" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose
    sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  else
    curl -fsSL https://get.docker.com | sudo sh
  fi
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$DEPLOY_USER"

# LangChain + pandas need ~1 GB; give 1-2 GB instances some swap so a spike doesn't OOM-kill the API.
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$mem_mb" -lt 3500 ] && ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

sudo mkdir -p "$APP_DIR"
sudo chown "$DEPLOY_USER":"$DEPLOY_USER" "$APP_DIR"
chmod 750 "$APP_DIR"

echo "✓ Docker $(sudo docker --version | cut -d' ' -f3 | tr -d ,) and compose $(sudo docker compose version --short) ready; $APP_DIR owned by $DEPLOY_USER."
echo "  Log out and back in once so '$DEPLOY_USER' can run docker without sudo."
