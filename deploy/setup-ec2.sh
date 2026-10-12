#!/usr/bin/env bash
# One-time server setup for Ubuntu 24.04 on AWS EC2 (t3.small, 2 GB RAM).
# Run on the server:   sudo bash setup-ec2.sh
# Idempotent: safe to run again.
#
# Installs Docker Engine + Compose plugin (official Docker repo), adds a 2 GB swap file
# (n8n + Postgres + the AI API fit in 2 GB RAM, swap absorbs build spikes), enables the
# UFW firewall (22/80/443 only), automatic security updates, and the AWS CLI for backups.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run with sudo"; exit 1; fi
APP_USER="${SUDO_USER:-ubuntu}"
APP_DIR=/opt/screener

echo "==> System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl gnupg ufw unattended-upgrades unzip cron

echo "==> Docker Engine (official repository)"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
usermod -aG docker "$APP_USER"   # lets the ubuntu user run docker without sudo (re-login needed)

echo "==> 2 GB swap file"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -w vm.swappiness=10 >/dev/null
grep -q 'vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf

echo "==> Firewall (UFW): SSH, HTTP, HTTPS only"
# The AWS security group is the first wall (SSH only from your IP); UFW is the second.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> Automatic security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> AWS CLI v2 (for nightly backups to S3)"
if ! command -v aws >/dev/null 2>&1; then
  tmp=$(mktemp -d)
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o "$tmp/awscliv2.zip"
  unzip -q "$tmp/awscliv2.zip" -d "$tmp"
  "$tmp/aws/install" --update
  rm -rf "$tmp"
fi

echo "==> App directory $APP_DIR"
mkdir -p "$APP_DIR/backups"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Timezone"
timedatectl set-timezone Asia/Karachi

echo
echo "Done. Log out and back in (so 'docker' works without sudo), then continue with deploy/README.md."
docker --version
docker compose version
