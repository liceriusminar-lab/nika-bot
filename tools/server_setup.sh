#!/usr/bin/env bash
# One-time server preparation (Ubuntu 24.04). Run via: tools\deploy.ps1 -Server root@IP -Setup
# Installs Docker, sets the timezone and creates ~/nika. Safe to run again.
set -euo pipefail

echo "== Updating package lists"
apt-get update -y
apt-get install -y curl ca-certificates

if ! command -v docker >/dev/null 2>&1; then
  echo "== Installing Docker"
  curl -fsSL https://get.docker.com | sh
fi

# Docker Hub is sometimes unreachable from some networks; Google's mirror serves the same images
if [ ! -f /etc/docker/daemon.json ]; then
  echo '{ "registry-mirrors": ["https://mirror.gcr.io"] }' > /etc/docker/daemon.json
fi

systemctl enable docker
systemctl restart docker

timedatectl set-timezone Asia/Vladivostok || true
mkdir -p ~/nika/data

echo "== Done"
docker --version
docker compose version
