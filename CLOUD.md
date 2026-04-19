# Cloud Deployment — Google Cloud Platform

## Overview

The weather radar app runs on a single GCE (Google Compute Engine) VM with Docker Compose. Caddy handles TLS termination and reverse proxies to the FastAPI app container. All deployment is managed through `deploy.sh`.

```
┌─────────────────────────────────────────────────────────┐
│  GCE VM: weather-radar (e2-medium, us-east4-c)         │
│  Static IP: 34.150.245.96                               │
│                                                         │
│  ┌─────────────────────────────────────────────┐        │
│  │  Caddy (caddy:2-alpine)                     │        │
│  │  :80 → reverse_proxy app:8080               │        │
│  │  :443 → auto-TLS for wx.somefamilies.com    │        │
│  └──────────────────┬──────────────────────────┘        │
│                     │                                   │
│  ┌──────────────────▼──────────────────────────┐        │
│  │  App (weather-radar-app)                    │        │
│  │  Python 3.11 + FastAPI + uvicorn :8080      │        │
│  │  Serves API + frontend dist                 │        │
│  │  Volume: ./data:/data (persistent cache)    │        │
│  └─────────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────────┘
```

## Infrastructure

| Resource | Value |
|----------|-------|
| GCP Project | `weather-radar-prod` |
| VM Name | `weather-radar` |
| Machine Type | `e2-medium` (2 vCPU, 4 GB RAM) |
| Zone | `us-east4-c` |
| OS | Debian 12 |
| Boot Disk | 20 GB |
| Static IP | `34.150.245.96` (reserved as `weather-radar-ip`) |
| Domain | `wx.somefamilies.com` (DNS A record → static IP) |
| Firewall | `allow-http-https` rule: TCP 80, 443 with tag `http-server` |
| OS Login | Enabled project-wide for service account SSH |

## Service Account

A dedicated service account handles non-interactive deployments so you never need `gcloud auth login`.

| Field | Value |
|-------|-------|
| Email | `deployer@weather-radar-prod.iam.gserviceaccount.com` |
| Key File | `.gcp-key.json` (project root, gitignored) |
| Configured via | `GCP_SA_KEY=.gcp-key.json` in `.env` |

**IAM Roles:**

| Role | Purpose |
|------|---------|
| `roles/compute.instanceAdmin.v1` | Start/stop VM, manage SSH keys |
| `roles/compute.osAdminLogin` | SSH into VM with sudo via OS Login |
| `roles/iam.serviceAccountUser` | Act as default compute service account |
| `roles/iap.tunnelResourceAccessor` | SSH tunnel access |

**Auth flow in `deploy.sh`:**
1. Reads `GCP_SA_KEY` from `.env` (or falls back to `.gcp-key.json` in project root)
2. Calls `gcloud auth activate-service-account --key-file=...`
3. When SSHing as the SA, OS Login creates user `sa_XXXXXXXXXX` which requires `sudo` for docker/git
4. `deploy.sh` detects SA auth and automatically prefixes remote commands with `sudo`
5. If no key file is found, falls back to whatever gcloud credentials are active

**Regenerating the key** (if compromised or expired):
```bash
# Revoke old key
gcloud iam service-accounts keys list \
  --iam-account=deployer@weather-radar-prod.iam.gserviceaccount.com
gcloud iam service-accounts keys delete KEY_ID \
  --iam-account=deployer@weather-radar-prod.iam.gserviceaccount.com

# Create new key
gcloud iam service-accounts keys create .gcp-key.json \
  --iam-account=deployer@weather-radar-prod.iam.gserviceaccount.com
```

## Docker Setup

### Dockerfile (multi-stage)

1. **Stage 1 (frontend):** `node:20-slim` — runs `npm ci` + `npm run build` to produce the minified frontend bundle in `dist/`
2. **Stage 2 (runtime):** `python:3.11-slim` — installs backend pip dependencies, copies backend code + frontend dist, runs uvicorn on port 8080

### docker-compose.prod.yml

Two services:
- **caddy** — Caddy 2 reverse proxy. Listens on ports 80/443, auto-provisions TLS certificates for `wx.somefamilies.com` via Let's Encrypt. Config in `Caddyfile`. Persistent volumes for TLS certs (`caddy_data`, `caddy_config`).
- **app** — The weather radar application. Exposes port 8080 to the Docker network only (not host). Mounts `./data:/data` for persistent MRMS cache. Reads API keys from `.env` on the VM.

### docker-compose.yml (local dev)

Single `app` service, maps host port 8000 → container port 8080. No Caddy, no TLS.

## Deploying

### Standard deploy (push code changes)

```bash
./deploy.sh deploy
```

This:
1. Authenticates via the service account key
2. SSHs into the VM and runs `git pull` in `/opt/weather-radar/`
3. Runs `docker compose -f docker-compose.prod.yml up -d --build` to rebuild the image and restart the app container
4. Waits 5 seconds and checks `/health`

The Docker build is fast (~10s) because pip dependencies and npm dependencies are cached in Docker layers. Only the `COPY backend/` and `COPY frontend/` steps re-run when code changes.

After restart the app seeds its MRMS frame cache from S3 (1-2 minutes for 60 frames). The frontend retries automatically during this time.

### Other commands

```bash
./deploy.sh status    # Container status + health check
./deploy.sh logs      # Tail production logs (Ctrl+C to exit)
./deploy.sh ssh       # Open a shell on the VM
./deploy.sh stop      # Stop containers + VM (saves money)
./deploy.sh start     # Start VM + containers
./deploy.sh setup     # One-time: create VM, reserve IP, install Docker, clone repo
```

### Typical workflow

```bash
# 1. Make changes locally, test with local dev server
uvicorn backend.main:app

# 2. Commit and push to GitHub
git add -A && git commit -m "description" && git push

# 3. Deploy to production
./deploy.sh deploy

# 4. Verify
./deploy.sh status
```

## VM Layout

```
/opt/weather-radar/              ← git repo (cloned from GitHub)
├── backend/                     ← Python backend
├── frontend/                    ← Frontend source (dist/ built inside Docker)
├── data/                        ← Persistent MRMS cache (mounted as Docker volume)
│   ├── raw/{tilt}/              ← Raw .grib2.gz from S3
│   └── tilt_grids/{timestamp}/  ← Sparse CSR grids + motion fields
├── .env                         ← API keys (STADIA_API_KEY, MAPTILER_API_KEY)
├── Caddyfile                    ← Caddy reverse proxy config
├── docker-compose.prod.yml      ← Production compose (Caddy + app)
└── Dockerfile                   ← Multi-stage build
```

The `data/` directory persists across container rebuilds since it's a bind mount. It contains the MRMS radar cache (~300-400 MB for 60 frames). On a fresh deploy the app re-seeds from S3; on a restart it warms from the existing disk cache.

## TLS / HTTPS

Caddy automatically provisions and renews TLS certificates from Let's Encrypt for `wx.somefamilies.com`. No manual certificate management needed. The `Caddyfile` is minimal:

```
wx.somefamilies.com {
    reverse_proxy app:8080
}

:80 {
    reverse_proxy app:8080
}
```

The `:80` block handles plain HTTP for the health check endpoint on localhost. External HTTP requests to `wx.somefamilies.com` are automatically redirected to HTTPS by Caddy.

## DNS

An A record points `wx.somefamilies.com` to the VM's static IP (`34.150.245.96`). This was configured in the domain registrar's DNS settings.

## Cost

The `e2-medium` VM in `us-east4-c` costs approximately $25/month when running continuously. Use `./deploy.sh stop` to stop the VM when not needed (no compute charges while stopped; the static IP incurs a small charge of ~$7/month when not attached to a running VM).

## Troubleshooting

**App not responding after deploy:**
The app takes 1-2 minutes to seed its MRMS cache from S3 on startup. Check logs with `./deploy.sh logs`.

**Docker permission denied:**
If using the service account, commands are automatically prefixed with `sudo`. If SSHing manually, your user needs to be in the docker group: `sudo usermod -aG docker $USER` (then re-login).

**TLS certificate issues:**
Caddy manages certificates automatically. If something goes wrong, check Caddy logs: `./deploy.sh ssh` then `docker logs weather-radar-caddy-1`.

**Disk space:**
The MRMS cache is bounded by the pipeline's 3-hour eviction policy and 60-frame cap. Total disk use is typically under 1 GB. Check with `./deploy.sh ssh` then `du -sh /opt/weather-radar/data/`.

**Regenerating the service account key:**
See the "Service Account" section above. After generating a new `.gcp-key.json`, no other changes are needed.
