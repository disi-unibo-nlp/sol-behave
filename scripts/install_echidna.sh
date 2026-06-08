#!/usr/bin/env bash
# install_echidna.sh
# Installs echidna 2.0.2 (the latest available via npm) as echidna-bin
# on Linux x86_64 where the GitHub release CDN is blocked.
#
# Usage:  bash install_echidna.sh
# Requires: npm, root or sudo

set -e

echo "Installing @crytic/echidna via npm..."
npm install -g @crytic/echidna

ECHIDNA_BIN="$(npm root -g)/@crytic/echidna/bin/echidna/2.0.2/Ubuntu-18.04/echidna-test"

if [ ! -f "$ECHIDNA_BIN" ]; then
    echo "ERROR: expected binary not found at $ECHIDNA_BIN"
    echo "Available files:"
    find "$(npm root -g)/@crytic/echidna/bin" -type f
    exit 1
fi

chmod +x "$ECHIDNA_BIN"
cp "$ECHIDNA_BIN" /usr/local/bin/echidna-bin
echo "Installed: $(echidna-bin --version 2>&1 | head -1)"
echo "echidna-bin is ready."