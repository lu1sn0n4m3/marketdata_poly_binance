# Infrastructure Setup: VPS & S3 Architecture

This document details the complete infrastructure setup for Polymarket market making with Binance signals, including separate production and data collection environments.

## Overview

The infrastructure is split into two isolated environments:

1. **Production**: Live trading with minimal latency
2. **Collection**: Data gathering for backtesting

Both environments mirror the same latency characteristics to ensure backtests are realistic.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              PRODUCTION (Live Trading)                               │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                      │
│   ┌─────────────────────────────┐         ┌─────────────────────────────────────┐   │
│   │  FRANKFURT VPS              │         │  AMSTERDAM VPS                      │   │
│   │  binance-sender             │  3.5ms  │  trading-executor                   │   │
│   │  ─────────────────────────  │ ──────> │  ─────────────────────────────────  │   │
│   │                             │   TLS   │                                     │   │
│   │  Docker: binance-sender     │         │  Docker: trading-executor           │   │
│   │  - Connects to Binance WS   │         │  - Receives Binance signals         │   │
│   │  - Streams BBO to Amsterdam │         │  - Executes orders on Polymarket    │   │
│   │                             │         │  - Monitors Polymarket WS           │   │
│   │  Specs: 1 vCPU, 1GB RAM     │         │  Specs: 1 vCPU, 2GB RAM             │   │
│   │  Cost: $6/month             │         │  Cost: $12/month                    │   │
│   └─────────────────────────────┘         └─────────────────────────────────────┘   │
│                                                                                      │
│   Total Production Cost: $18/month                                                  │
└─────────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              COLLECTION (Backtesting Data)                           │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                      │
│   ┌─────────────────────────────┐         ┌─────────────────────────────────────┐   │
│   │  FRANKFURT VPS              │         │  AMSTERDAM VPS                      │   │
│   │  binance-collector          │  3.5ms  │  data-collector                     │   │
│   │  ─────────────────────────  │ ──────> │  ─────────────────────────────────  │   │
│   │                             │   TLS   │                                     │   │
│   │  Docker: binance-collector  │         │  Docker: data-collector             │   │
│   │  - Connects to Binance WS   │         │  - Receives Binance from Frankfurt  │   │
│   │  - Streams to Amsterdam     │         │  - Connects to Polymarket WS        │   │
│   │  - BBO + Trades             │         │  - Collects L2 + Trades             │   │
│   │                             │         │  - Writes Parquet locally           │   │
│   │  Specs: 1 vCPU, 1GB RAM     │         │  - Uploads to S3 hourly             │   │
│   │  Cost: $6/month             │         │                                     │   │
│   │                             │         │  Specs: 2 vCPU, 4GB RAM             │   │
│   │                             │         │  Cost: $24/month                    │   │
│   └─────────────────────────────┘         └─────────────────────────────────────┘   │
│                                                         │                            │
│   Total Collection Cost: $30/month                      │                            │
│                                                         ▼                            │
│                                           ┌─────────────────────────────────────┐   │
│                                           │  AWS S3 - eu-central-1              │   │
│                                           │  Bucket: marketdata-backtest        │   │
│                                           │  ─────────────────────────────────  │   │
│                                           │  - Parquet files                    │   │
│                                           │  - Partitioned by date/symbol       │   │
│                                           │  - Lifecycle: Standard → IA → Glacier│  │
│                                           │  Cost: ~$2-5/month (100GB)          │   │
│                                           └─────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────────┘

TOTAL INFRASTRUCTURE COST: ~$50-55/month
```

---

## VPS Specifications

| VPS Name | Location | Environment | Purpose | Specs | Monthly Cost |
|----------|----------|-------------|---------|-------|--------------|
| `binance-sender` | Frankfurt (fra1) | Production | Stream Binance → Amsterdam | 1 vCPU, 1GB RAM, 25GB SSD | $6 |
| `trading-executor` | Amsterdam (ams3) | Production | Execute trades on Polymarket | 1 vCPU, 2GB RAM, 50GB SSD | $12 |
| `binance-collector` | Frankfurt (fra1) | Collection | Stream Binance for backtest | 1 vCPU, 1GB RAM, 25GB SSD | $6 |
| `data-collector` | Amsterdam (ams3) | Collection | Collect all data, upload S3 | 2 vCPU, 4GB RAM, 80GB SSD | $24 |

### Why These Locations?

| Location | Binance Access | Polymarket Trading | Role |
|----------|----------------|-------------------|------|
| Frankfurt (Germany) | ✅ Yes | ❌ Blocked | Binance data source |
| Amsterdam (Netherlands) | ❌ Blocked | ✅ Yes | Polymarket trading |
| London (UK) | ✅ Yes | ❌ Blocked | Not used |

**Latency Profile:**
- Binance (Tokyo) → Frankfurt: ~127ms
- Frankfurt → Amsterdam: ~3.5ms
- Amsterdam → Polymarket API: ~40-50ms RTT

---

## Docker Containers

### Container Summary

| Container | Runs On | Image | Ports | Volumes |
|-----------|---------|-------|-------|---------|
| `binance-sender` | Frankfurt (prod) | `streaming/sender` | None (outbound only) | `/certs` |
| `trading-executor` | Amsterdam (prod) | `executor/` | `9001` (receive) | `/certs`, `/config` |
| `binance-collector` | Frankfurt (collect) | `streaming/sender` | None (outbound only) | `/certs` |
| `data-collector` | Amsterdam (collect) | `collector/` | `9001` (receive) | `/certs`, `/data`, `/config` |

### Container 1: binance-sender (Production)

**Location:** Frankfurt VPS (Production)
**Purpose:** Stream Binance BBO to trading executor

```
streaming/
├── sender/
│   ├── Dockerfile
│   ├── sender.py
│   └── requirements.txt
└── docker-compose.sender.yml
```

**docker-compose.sender.yml:**
```yaml
services:
  binance-sender:
    build: ./sender
    container_name: binance-sender
    restart: always
    environment:
      - AMS_HOST=<trading-executor-ip>
      - AMS_PORT=9001
      - SYMBOLS=btcusdt
    volumes:
      - ./certs:/certs:ro
```

---

### Container 2: trading-executor (Production)

**Location:** Amsterdam VPS (Production)
**Purpose:** Receive Binance signals, execute on Polymarket

```
executor/
├── Dockerfile
├── requirements.txt
├── executor.py          # Main trading logic
├── receiver.py          # Binance signal receiver
└── polymarket_client.py # Order execution
```

**Dockerfile:**
```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 9001

CMD ["python", "-u", "executor.py"]
```

**docker-compose.executor.yml:**
```yaml
services:
  trading-executor:
    build: ./executor
    container_name: trading-executor
    restart: always
    ports:
      - "9001:9001"
    environment:
      - POLY_PRIVATE_KEY=${POLY_PRIVATE_KEY}
      - POLY_FUNDER=${POLY_FUNDER}
      - TZ=UTC
    volumes:
      - ./certs:/certs:ro
      - ./config:/config:ro
```

---

### Container 3: binance-collector (Collection)

**Location:** Frankfurt VPS (Collection)
**Purpose:** Stream Binance BBO+Trades to data collector

Same as `binance-sender` but configured to send to `data-collector` IP and includes trades:

```yaml
services:
  binance-collector:
    build: ./sender
    container_name: binance-collector
    restart: always
    environment:
      - AMS_HOST=<data-collector-ip>
      - AMS_PORT=9001
      - SYMBOLS=btcusdt,ethusdt
      - INCLUDE_TRADES=true  # Additional env var
    volumes:
      - ./certs:/certs:ro
```

---

### Container 4: data-collector (Collection)

**Location:** Amsterdam VPS (Collection)
**Purpose:** Collect all data, write to S3

```
collector/
├── Dockerfile
├── requirements.txt
├── pyproject.toml
└── src/
    └── collector/
        ├── main.py           # Entry point
        ├── streams/
        │   ├── binance.py    # Binance receiver
        │   └── polymarket.py # Polymarket WS client
        ├── writers/
        │   ├── buffer.py     # In-memory buffer
        │   ├── parquet.py    # Write parquet files
        │   └── s3.py         # S3 upload
        └── schemas/
            ├── bbo.py
            ├── trade.py
            └── book.py
```

**docker-compose.collector.yml:**
```yaml
services:
  data-collector:
    build: ./collector
    container_name: data-collector
    restart: always
    ports:
      - "9001:9001"
    environment:
      - AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
      - AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}
      - S3_BUCKET=marketdata-backtest
      - S3_REGION=eu-central-1
      - POLYMARKET_TOKENS=token1,token2  # Token IDs to collect
      - TZ=UTC
    volumes:
      - ./certs:/certs:ro
      - ./data:/data  # Local buffer before S3 upload
```

---

## S3 Configuration

### Bucket Setup

**Bucket Name:** `marketdata-backtest`
**Region:** `eu-central-1` (Frankfurt) - closest to Amsterdam for low-latency uploads

### Directory Structure

```
s3://marketdata-backtest/
├── binance/
│   ├── bbo/
│   │   └── symbol=BTCUSDT/
│   │       └── date=2026-01-19/
│   │           ├── hour=00/
│   │           │   └── data_000.parquet
│   │           ├── hour=01/
│   │           │   └── data_000.parquet
│   │           └── ...
│   └── trades/
│       └── symbol=BTCUSDT/
│           └── date=2026-01-19/
│               └── hour=00/
│                   └── data_000.parquet
│
└── polymarket/
    ├── l2/
    │   └── token=80339409420962.../
    │       └── date=2026-01-19/
    │           └── hour=00/
    │               └── data_000.parquet
    └── trades/
        └── token=80339409420962.../
            └── date=2026-01-19/
                └── hour=00/
                    └── data_000.parquet
```

### Parquet Schema

**Binance BBO:**
```
recv_ts: int64 (nanoseconds, local receive time)
exch_ts: int64 (milliseconds, Binance event time)
symbol: string
bid: float64
ask: float64
bid_qty: float64
ask_qty: float64
```

**Binance Trades:**
```
recv_ts: int64
exch_ts: int64
symbol: string
price: float64
qty: float64
side: string (buy/sell)
trade_id: int64
```

**Polymarket L2:**
```
recv_ts: int64
token_id: string
bids: list<struct<price: float64, size: float64>>
asks: list<struct<price: float64, size: float64>>
```

**Polymarket Trades:**
```
recv_ts: int64
token_id: string
price: float64
size: float64
side: string
```

### Lifecycle Rules

```json
{
  "Rules": [
    {
      "ID": "ArchiveOldData",
      "Status": "Enabled",
      "Filter": {},
      "Transitions": [
        {
          "Days": 30,
          "StorageClass": "STANDARD_IA"
        },
        {
          "Days": 90,
          "StorageClass": "GLACIER"
        }
      ]
    }
  ]
}
```

### IAM Policy for Collector

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:DeleteObject"
      ],
      "Resource": [
        "arn:aws:s3:::marketdata-backtest",
        "arn:aws:s3:::marketdata-backtest/*"
      ]
    }
  ]
}
```

---

## Deployment Checklist

### Prerequisites

- [ ] DigitalOcean account with API token
- [ ] AWS account with S3 access
- [ ] Domain or static IPs for VPSs
- [ ] TLS certificates generated (see `streaming/scripts/generate-certs.sh`)

### Step 1: Create VPSs

```bash
# Using doctl (DigitalOcean CLI)

# Production - Frankfurt
doctl compute droplet create binance-sender \
  --region fra1 \
  --size s-1vcpu-1gb \
  --image docker-20-04

# Production - Amsterdam
doctl compute droplet create trading-executor \
  --region ams3 \
  --size s-1vcpu-2gb \
  --image docker-20-04

# Collection - Frankfurt
doctl compute droplet create binance-collector \
  --region fra1 \
  --size s-1vcpu-1gb \
  --image docker-20-04

# Collection - Amsterdam
doctl compute droplet create data-collector \
  --region ams3 \
  --size s-2vcpu-4gb \
  --image docker-20-04
```

### Step 2: Generate TLS Certificates

```bash
cd streaming/scripts
chmod +x generate-certs.sh

# For production
./generate-certs.sh <trading-executor-ip>
mv ../certs ../certs-prod

# For collection
./generate-certs.sh <data-collector-ip>
mv ../certs ../certs-collect
```

### Step 3: Create S3 Bucket

```bash
# Create bucket
aws s3 mb s3://marketdata-backtest --region eu-central-1

# Apply lifecycle rules
aws s3api put-bucket-lifecycle-configuration \
  --bucket marketdata-backtest \
  --lifecycle-configuration file://lifecycle.json

# Create IAM user and get credentials
aws iam create-user --user-name marketdata-collector
aws iam put-user-policy --user-name marketdata-collector \
  --policy-name S3Access --policy-document file://iam-policy.json
aws iam create-access-key --user-name marketdata-collector
# Save the AccessKeyId and SecretAccessKey!
```

### Step 4: Deploy Production

**Frankfurt (binance-sender):**
```bash
ssh root@<binance-sender-ip>
mkdir -p /opt/streaming
# Copy files...
cd /opt/streaming
docker compose -f docker-compose.sender.yml up -d --build
```

**Amsterdam (trading-executor):**
```bash
ssh root@<trading-executor-ip>
mkdir -p /opt/executor
# Copy files...
cd /opt/executor
docker compose -f docker-compose.executor.yml up -d --build
```

### Step 5: Deploy Collection

**Frankfurt (binance-collector):**
```bash
ssh root@<binance-collector-ip>
mkdir -p /opt/streaming
# Copy files with collection config...
cd /opt/streaming
docker compose -f docker-compose.collector.yml up -d --build
```

**Amsterdam (data-collector):**
```bash
ssh root@<data-collector-ip>
mkdir -p /opt/collector
# Copy files...
cd /opt/collector
# Set environment variables
export AWS_ACCESS_KEY_ID=<your-key>
export AWS_SECRET_ACCESS_KEY=<your-secret>
docker compose -f docker-compose.collector.yml up -d --build
```

### Step 6: Verify

```bash
# Check containers are running
docker ps

# Check logs
docker logs -f binance-sender
docker logs -f data-collector

# Check S3 uploads (after 1 hour)
aws s3 ls s3://marketdata-backtest/binance/bbo/ --recursive
```

---

## Monitoring

### Health Checks

Each container includes a health check. Monitor with:

```bash
docker inspect --format='{{.State.Health.Status}}' <container-name>
```

### Log Aggregation (Optional)

Add to docker-compose for centralized logging:

```yaml
services:
  <service>:
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

### Alerts (Optional)

Set up DigitalOcean Monitoring alerts for:
- CPU > 80% for 5 minutes
- Memory > 90%
- Disk > 80%

---

## Cost Summary

| Component | Monthly Cost |
|-----------|-------------|
| binance-sender (Frankfurt, 1GB) | $6 |
| trading-executor (Amsterdam, 2GB) | $12 |
| binance-collector (Frankfurt, 1GB) | $6 |
| data-collector (Amsterdam, 4GB) | $24 |
| S3 Storage (~100GB) | $2.30 |
| S3 Transfer (download for backtest) | ~$5-10 |
| **Total** | **~$55-60/month** |

### Cost Optimization Options

1. **Consolidate Frankfurt VPSs**: Use one Frankfurt VPS for both sender and collector ($6 saved)
2. **Reduce collection VPS**: If data volume is low, use 2GB instead of 4GB ($12 saved)
3. **S3 Intelligent Tiering**: Automatically moves data to cheaper storage

---

## Disaster Recovery

### Backup Strategy

1. **Configuration**: Store all docker-compose files and configs in Git
2. **Certificates**: Store encrypted copies in S3 or password manager
3. **Data**: S3 provides 99.999999999% durability

### Recovery Steps

1. Create new VPS in same region
2. Pull Docker images from registry (or rebuild)
3. Restore certificates
4. Start containers
5. Verify connectivity

### Failover

For production trading, consider:
- DigitalOcean Reserved IPs for quick failover
- Health check scripts that alert on failure
- Runbook for manual intervention

---

## Security Checklist

- [ ] TLS certificates for all inter-VPS communication
- [ ] Firewall rules: only allow required ports between VPSs
- [ ] SSH key-only authentication (disable password)
- [ ] Environment variables for secrets (never in code)
- [ ] S3 bucket policy restricts to VPS IPs
- [ ] Regular security updates: `apt update && apt upgrade`
- [ ] Fail2ban for SSH protection
