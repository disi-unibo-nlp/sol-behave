#!/bin/bash
set -euo pipefail

# CALL THIS SCRIPT BEFORE RUNNING ANYTHING ELSE

echo "=== Installing solc-select ==="

pip uninstall -y solc-select || true
pip install --upgrade solc-select

echo "=== Installing NVM 0.40.4 ==="

curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

echo "=== Installing Node.js (npm 11.9.0) ==="

# npm 11.9.0 ships with Node 24.x
nvm install 24
nvm use 24
nvm alias default 24

echo "=== Installing Hardhat 2.28.6 ==="

# Remove any global Hardhat that could confuse the environment
npm uninstall -g hardhat || true

# Create local package if missing
if [ ! -f package.json ]; then
    npm init -y
fi

# Force the exact Hardhat version
npm install --save-dev hardhat@2.28.6

echo "=== Installing Solidity compiler 0.4.18 ==="

solc-select install 0.4.18 || true
solc-select use 0.4.18

echo
echo "======================================="
echo "Installed versions:"
echo "======================================="

echo "Node:"
node -v

echo "npm:"
npm -v

echo "npx:"
npx -v

echo "nvm:"
nvm --version

echo "Hardhat:"
npx hardhat --version

echo "solc:"
solc --version

echo "solc-select:"
solc-select versions

echo
echo "Now run:"
echo "source ~/.bashrc"
echo "source .env"