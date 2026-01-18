# Collector Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Copy everything needed for install
COPY pyproject.toml .
COPY src/ src/

# Install the package
RUN pip install --no-cache-dir .

# Create non-root user
RUN useradd -m -u 1000 collector
USER collector

# Data volume mount point
VOLUME /data

# Environment
ENV COLLECTOR_DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

# Run collector
CMD ["collector"]
