# Uploader Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Copy everything needed for install
COPY pyproject.toml .
COPY src/ src/

# Install the package
RUN pip install --no-cache-dir .

# Create non-root user
RUN useradd -m -u 1000 uploader
USER uploader

# Data volume mount point
VOLUME /data

# Environment defaults (override via env_file or environment)
ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

# Run uploader
CMD ["uploader"]
