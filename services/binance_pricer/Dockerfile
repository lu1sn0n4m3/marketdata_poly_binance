# Container A: Binance Pricer
# Dockerfile for the Binance data ingestion and pricing container

FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy shared code
COPY shared/ /app/shared/

# Copy requirements and install dependencies
COPY services/binance_pricer/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY services/binance_pricer/src/ /app/src/

# Set Python path to include shared modules
ENV PYTHONPATH=/app:/app/src

# Default environment variables
ENV BINANCE_SYMBOL=BTCUSDT
ENV HTTP_HOST=0.0.0.0
ENV HTTP_PORT=8080
ENV STALE_THRESHOLD_MS=500
ENV SNAPSHOT_PUBLISH_HZ=50
ENV LOG_LEVEL=INFO

# Expose HTTP port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Run the application
CMD ["python", "-m", "binance_pricer.app"]
