# Container B: Polymarket Trader
# Dockerfile for the trading, risk management, and execution container

FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy shared code
COPY shared/ /app/shared/

# Copy requirements and install dependencies
COPY services/polymarket_trader/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY services/polymarket_trader/src/ /app/src/

# Set Python path to include shared modules
ENV PYTHONPATH=/app:/app/src

# Create data directory for SQLite journal
RUN mkdir -p /data

# Default environment variables
ENV POLL_SNAPSHOT_HZ=50
ENV SNAPSHOT_URL=http://binance_pricer:8080/snapshot/latest
ENV GAMMA_API_URL=https://gamma-api.polymarket.com
# Base market slug - date/time appended automatically (e.g., bitcoin-up-or-down-january-22-7am-et)
ENV MARKET_SLUG=bitcoin-up-or-down
ENV SQLITE_PATH=/data/journal.db
ENV CONTROL_HOST=0.0.0.0
ENV CONTROL_PORT=9000
ENV LOG_LEVEL=INFO
# Risk limits (override in prod.env)
ENV MAX_RESERVED_CAPITAL=5.0
ENV MAX_POSITION_SIZE=5.0
ENV MAX_ORDER_SIZE=5.0
ENV MAX_OPEN_ORDERS=4

# Expose control plane port
EXPOSE 9000

# Health check using control plane
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9000/status')" || exit 1

# Run the application
CMD ["python", "-m", "polymarket_trader.app"]
