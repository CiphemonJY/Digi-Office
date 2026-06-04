#!/usr/bin/env bash
# Run on Hermes (WSL) to install and start the coordinator.
set -euo pipefail

DIGI_DIR="$HOME/digi_office"
SERVICE_DIR="$HOME/.config/systemd/user"

echo "=== Digi-Office Hermes Setup ==="

# 1. Dependencies
pip install --quiet fastapi "uvicorn[standard]" pydantic requests

# 2. Systemd user service
mkdir -p "$SERVICE_DIR"
cp "$DIGI_DIR/deploy/digi-office.service" "$SERVICE_DIR/digi-office.service"
systemctl --user daemon-reload
systemctl --user enable digi-office
systemctl --user restart digi-office

echo "Coordinator running. Check status:"
echo "  systemctl --user status digi-office"
echo "  curl http://localhost:8080/health"
