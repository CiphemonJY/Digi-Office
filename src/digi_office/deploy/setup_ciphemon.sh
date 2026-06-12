#!/usr/bin/env bash
# Run on Ciphemon (Mac) to install the agent.
set -euo pipefail

WORKSPACE="$HOME/.config/digi-office/workspace/digi_office"
SCRIPTS="$HOME/.config/digi-office/scripts"
LOGS="$HOME/.config/digi-office/logs"
PLIST="$HOME/Library/LaunchAgents/digi-office.agent.plist"

echo "=== Digi-Office Ciphemon Setup ==="

# 1. Create dirs
mkdir -p "$WORKSPACE" "$SCRIPTS" "$LOGS"

# 2. Copy SDK + agent
cp -r agent_sdk/ "$WORKSPACE/"
cp deploy/ciphemon_agent.py "$SCRIPTS/"

# 3. Install deps
pip3 install --quiet requests

# 4. Register launchd service
cp deploy/digi-office.agent.plist "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Agent registered. Check logs:"
echo "  tail -f $LOGS/digi-office.log"
