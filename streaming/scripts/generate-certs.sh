#!/bin/bash
#
# Generate TLS certificates for sender/receiver communication
# Run this once and distribute the certs to both VPSs
#

set -e

CERT_DIR="../certs"
mkdir -p "$CERT_DIR"

# Certificate validity (days)
DAYS=3650  # 10 years

# Amsterdam receiver IP (update this!)
AMS_IP="${1:-146.190.30.198}"

echo "Generating TLS certificates for Amsterdam receiver ($AMS_IP)..."

# Generate private key
openssl genrsa -out "$CERT_DIR/server.key" 2048

# Generate self-signed certificate
openssl req -new -x509 \
    -key "$CERT_DIR/server.key" \
    -out "$CERT_DIR/server.crt" \
    -days "$DAYS" \
    -subj "/CN=$AMS_IP" \
    -addext "subjectAltName=IP:$AMS_IP"

# Copy server cert as CA cert for sender
cp "$CERT_DIR/server.crt" "$CERT_DIR/ams_server.crt"

echo ""
echo "Certificates generated in $CERT_DIR/"
echo ""
echo "Files:"
echo "  server.key       - Private key (Amsterdam only, keep secret!)"
echo "  server.crt       - Server certificate (Amsterdam)"
echo "  ams_server.crt   - CA certificate (Frankfurt, for verification)"
echo ""
echo "Deployment:"
echo "  Amsterdam: Copy server.key and server.crt to the certs/ folder"
echo "  Frankfurt: Copy ams_server.crt to the certs/ folder"
echo ""
