# VPS Deployment Guide

This guide covers deploying the marketdata collector and uploader to a VPS.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         GitHub                               │
│  ┌─────────────┐    push    ┌──────────────┐                │
│  │   Your Code │ ────────── │ GitHub Actions│                │
│  └─────────────┘            └──────┬───────┘                │
│                                    │ build & push            │
│                             ┌──────▼───────┐                │
│                             │     GHCR     │                │
│                             │ (images)     │                │
│                             └──────┬───────┘                │
└────────────────────────────────────┼────────────────────────┘
                                     │ docker pull
                              ┌──────▼───────┐
                              │     VPS      │
                              │ ┌──────────┐ │
                              │ │collector │ │──┐
                              │ └──────────┘ │  │ shared
                              │ ┌──────────┐ │  │ /data
                              │ │ uploader │ │──┘
                              │ └──────────┘ │
                              │      │       │
                              │      ▼       │
                              │  Hetzner S3  │
                              └──────────────┘
```

## Prerequisites

On your VPS:
- Docker installed
- Docker Compose installed
- GitHub Personal Access Token (PAT) with `read:packages` scope

## One-Time Setup

### 1. Create directory structure

```bash
ssh luis@your-vps

mkdir -p /home/luis/marketdata/data
cd /home/luis/marketdata
```

### 2. Create GitHub PAT for pulling images

1. Go to https://github.com/settings/tokens
2. Generate new token (classic)
3. Select scope: `read:packages`
4. Copy the token

### 3. Login to GHCR on VPS

```bash
# Replace YOUR_GITHUB_USERNAME and YOUR_PAT
echo "YOUR_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

### 4. Copy compose and env files

From your local machine:
```bash
cd /path/to/repo
scp deploy/docker-compose.prod.yml luis@your-vps:/home/luis/marketdata/docker-compose.yml
scp deploy/env.example luis@your-vps:/home/luis/marketdata/.env
```

### 5. Configure environment

On VPS, edit `/home/luis/marketdata/.env`:
```bash
nano /home/luis/marketdata/.env
```

Fill in:
- `GITHUB_OWNER` - your GitHub username (lowercase)
- `IMAGE_TAG` - use `latest` initially
- `S3_*` - your Hetzner S3 credentials
- `TELEGRAM_*` - your Telegram bot token and chat ID

## Deploy

### First deploy

```bash
cd /home/luis/marketdata
docker compose pull
docker compose up -d
```

### Check status

```bash
# View running containers
docker compose ps

# View logs
docker compose logs -f collector
docker compose logs -f uploader

# Check health files
cat /home/luis/marketdata/data/state/health/collector.json
cat /home/luis/marketdata/data/state/health/uploader.json
```

## Update to New Version

When you push to `main`, GitHub Actions builds new images.

### Deploy latest

```bash
cd /home/luis/marketdata
docker compose pull
docker compose up -d
docker image prune -f  # cleanup old images
```

### Deploy specific version (SHA)

1. Find the commit SHA from GitHub Actions output
2. Edit `.env`:
   ```
   IMAGE_TAG=abc123def  # first 7 chars of SHA
   ```
3. Deploy:
   ```bash
   docker compose pull
   docker compose up -d
   ```

## Rollback

1. Find the previous working SHA
2. Edit `.env` to set `IMAGE_TAG=<previous-sha>`
3. Deploy:
   ```bash
   docker compose pull
   docker compose up -d
   ```

## Troubleshooting

### Container won't start

```bash
# Check logs
docker compose logs collector
docker compose logs uploader

# Check if image was pulled
docker images | grep marketdata
```

### Can't pull images

```bash
# Re-login to GHCR
echo "YOUR_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

# Check if images exist
# Visit: https://github.com/YOUR_USERNAME?tab=packages
```

### Uploader can't connect to S3

Check `.env` has correct S3 credentials:
```bash
cat /home/luis/marketdata/.env | grep S3
```

### Data not persisting

Check volume mount:
```bash
ls -la /home/luis/marketdata/data/
docker compose exec collector ls -la /data/
```

## Useful Commands

```bash
# Stop everything
docker compose down

# Restart a specific service
docker compose restart uploader

# View resource usage
docker stats

# Clean up disk space
docker system prune -a

# Check backlog
find /home/luis/marketdata/data/final -name 'data.parquet' | wc -l
find /home/luis/marketdata/data/state/uploaded -name '*.ok' | wc -l
```

## Data Locations

| Path | Contents |
|------|----------|
| `/home/luis/marketdata/data/tmp/` | Checkpoint files (temporary) |
| `/home/luis/marketdata/data/final/` | Finalized parquet files |
| `/home/luis/marketdata/data/state/` | Health, markers, finalization state |
