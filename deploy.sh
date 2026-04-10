#!/usr/bin/env bash
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
PROJECT_ID="weather-radar-prod"
ZONE="us-east4-c"
VM_NAME="weather-radar"
MACHINE_TYPE="e2-small"
DOMAIN="wx.somefamilies.com"
REPO_URL="https://github.com/alexlinde/weather_radar.git"
APP_DIR="/opt/weather-radar"

# ── Helpers ──────────────────────────────────────────────────────────────────

gce_ssh() {
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --project="$PROJECT_ID" -- "$@"
}

gce_scp() {
  gcloud compute scp "$@" --zone="$ZONE" --project="$PROJECT_ID"
}

wait_for_ssh() {
  echo "Waiting for SSH to become available..."
  for i in $(seq 1 30); do
    if gce_ssh "true" 2>/dev/null; then
      return 0
    fi
    sleep 5
  done
  echo "ERROR: SSH not available after 150 seconds"
  exit 1
}

# ── setup: one-time VM provisioning ─────────────────────────────────────────

cmd_setup() {
  echo "==> Setting up GCE project and VM"
  echo ""

  # Ensure project is selected
  gcloud config set project "$PROJECT_ID" --quiet 2>/dev/null || true

  # Enable Compute Engine API
  echo "==> Enabling Compute Engine API..."
  gcloud services enable compute.googleapis.com --quiet

  # Reserve a static external IP
  echo "==> Reserving static IP..."
  if gcloud compute addresses describe "$VM_NAME-ip" --region="${ZONE%-*}" --project="$PROJECT_ID" &>/dev/null; then
    echo "    Static IP already exists."
  else
    gcloud compute addresses create "$VM_NAME-ip" \
      --region="${ZONE%-*}" \
      --project="$PROJECT_ID"
  fi
  STATIC_IP=$(gcloud compute addresses describe "$VM_NAME-ip" \
    --region="${ZONE%-*}" \
    --project="$PROJECT_ID" \
    --format='value(address)')
  echo "    Static IP: $STATIC_IP"

  # Create firewall rules for HTTP/HTTPS
  echo "==> Configuring firewall rules..."
  if gcloud compute firewall-rules describe allow-http-https --project="$PROJECT_ID" &>/dev/null; then
    echo "    Firewall rules already exist."
  else
    gcloud compute firewall-rules create allow-http-https \
      --project="$PROJECT_ID" \
      --allow=tcp:80,tcp:443 \
      --target-tags=http-server \
      --description="Allow HTTP and HTTPS"
  fi

  # Create the VM
  echo "==> Creating VM ($MACHINE_TYPE in $ZONE)..."
  if gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --project="$PROJECT_ID" &>/dev/null; then
    echo "    VM already exists."
  else
    gcloud compute instances create "$VM_NAME" \
      --zone="$ZONE" \
      --project="$PROJECT_ID" \
      --machine-type="$MACHINE_TYPE" \
      --image-family=debian-12 \
      --image-project=debian-cloud \
      --boot-disk-size=20GB \
      --tags=http-server \
      --address="$STATIC_IP" \
      --metadata=startup-script='#!/bin/bash
        apt-get update -qq
        apt-get install -y -qq ca-certificates curl git
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list
        apt-get update -qq
        apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
        systemctl enable docker
        systemctl start docker
        usermod -aG docker $(ls /home/ | head -1)
      '
  fi

  wait_for_ssh

  # Clone repo and set up the app directory
  echo "==> Setting up application on VM..."
  gce_ssh "sudo mkdir -p $APP_DIR && sudo chown \$(whoami):\$(whoami) $APP_DIR"
  gce_ssh "
    if [ -d $APP_DIR/.git ]; then
      cd $APP_DIR && git pull
    else
      git clone $REPO_URL $APP_DIR
    fi
  "

  # Create a minimal .env on the VM if it doesn't exist
  gce_ssh "
    if [ ! -f $APP_DIR/.env ]; then
      cat > $APP_DIR/.env << 'ENVEOF'
STADIA_API_KEY=
MAPTILER_API_KEY=
ENVEOF
      echo 'Created default .env — edit $APP_DIR/.env on the VM to add API keys.'
    fi
  "

  # Ensure the persistent data directory exists
  gce_ssh "mkdir -p $APP_DIR/data"

  # Build and start
  echo "==> Building and starting containers..."
  gce_ssh "cd $APP_DIR && docker compose -f docker-compose.prod.yml up -d --build"

  echo ""
  echo "========================================"
  echo "  Setup complete!"
  echo "========================================"
  echo ""
  echo "  Static IP:  $STATIC_IP"
  echo "  VM:         $VM_NAME ($ZONE)"
  echo ""
  echo "  Next steps:"
  echo "    1. Add a DNS A record:"
  echo "       Name:  wx"
  echo "       Type:  A"
  echo "       Value: $STATIC_IP"
  echo "       (in your DNS provider for somefamilies.com)"
  echo ""
  echo "    2. Wait for DNS propagation (5-30 min)"
  echo ""
  echo "    3. Visit https://$DOMAIN"
  echo "       (Caddy auto-provisions TLS on first request)"
  echo ""
  echo "    4. Optionally edit API keys on the VM:"
  echo "       ./deploy.sh ssh"
  echo "       nano $APP_DIR/.env"
  echo "       cd $APP_DIR && docker compose -f docker-compose.prod.yml up -d"
  echo ""
}

# ── deploy: push code changes to the VM ─────────────────────────────────────

cmd_deploy() {
  echo "==> Deploying to $VM_NAME..."

  gcloud config set project "$PROJECT_ID" --quiet 2>/dev/null || true

  echo "==> Pulling latest code..."
  gce_ssh "cd $APP_DIR && git pull"

  echo "==> Rebuilding and restarting containers..."
  gce_ssh "cd $APP_DIR && docker compose -f docker-compose.prod.yml up -d --build"

  echo "==> Waiting for health check..."
  sleep 5
  if gce_ssh "curl -sf http://localhost:8080/health" >/dev/null 2>&1; then
    echo "    Health check passed."
  else
    echo "    Health check pending (app may still be seeding — this is normal)."
    echo "    Check with: ./deploy.sh logs"
  fi

  echo ""
  echo "==> Deploy complete!"
  echo "    https://$DOMAIN"
}

# ── logs: tail production logs ───────────────────────────────────────────────

cmd_logs() {
  gcloud config set project "$PROJECT_ID" --quiet 2>/dev/null || true
  gce_ssh "cd $APP_DIR && docker compose -f docker-compose.prod.yml logs -f --tail=100"
}

# ── ssh: open a shell on the VM ──────────────────────────────────────────────

cmd_ssh() {
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --project="$PROJECT_ID"
}

# ── status: check service health ─────────────────────────────────────────────

cmd_status() {
  gcloud config set project "$PROJECT_ID" --quiet 2>/dev/null || true
  echo "==> Container status:"
  gce_ssh "cd $APP_DIR && docker compose -f docker-compose.prod.yml ps"
  echo ""
  echo "==> Health check:"
  gce_ssh "curl -s http://localhost:8080/health" | python3 -m json.tool 2>/dev/null || echo "(no response)"
}

# ── stop / start ─────────────────────────────────────────────────────────────

cmd_stop() {
  gcloud config set project "$PROJECT_ID" --quiet 2>/dev/null || true
  echo "==> Stopping containers..."
  gce_ssh "cd $APP_DIR && docker compose -f docker-compose.prod.yml down"
  echo "==> Stopping VM..."
  gcloud compute instances stop "$VM_NAME" --zone="$ZONE" --project="$PROJECT_ID" --quiet
  echo "    VM stopped. No compute charges while stopped."
}

cmd_start() {
  gcloud config set project "$PROJECT_ID" --quiet 2>/dev/null || true
  echo "==> Starting VM..."
  gcloud compute instances start "$VM_NAME" --zone="$ZONE" --project="$PROJECT_ID" --quiet
  wait_for_ssh
  echo "==> Starting containers..."
  gce_ssh "cd $APP_DIR && docker compose -f docker-compose.prod.yml up -d"
  echo "    VM and containers running."
}

# ── Main ─────────────────────────────────────────────────────────────────────

usage() {
  cat <<EOF
Usage: ./deploy.sh <command>

Commands:
  setup     One-time: create GCE VM, reserve IP, install Docker, clone repo, start app
  deploy    Pull latest code, rebuild, and restart containers (default)
  logs      Tail production logs
  ssh       Open a shell on the VM
  status    Show container status and health check
  stop      Stop containers and VM (saves money)
  start     Start VM and containers
  help      Show this message

EOF
}

COMMAND="${1:-deploy}"

case "$COMMAND" in
  setup)  cmd_setup  ;;
  deploy) cmd_deploy ;;
  logs)   cmd_logs   ;;
  ssh)    cmd_ssh    ;;
  status) cmd_status ;;
  stop)   cmd_stop   ;;
  start)  cmd_start  ;;
  help|-h|--help) usage ;;
  *)
    echo "Unknown command: $COMMAND"
    usage
    exit 1
    ;;
esac
