#!/bin/bash

# CALL THIS SCRIPT BEFORE RUNNING ANYTHING ELSE

pip uninstall solc-select -y
pip install --upgrade solc-select

curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
nvm install --lts

# ______________________________________________________________________________________________________

# after running this script, run the following:
# source ~/.bashrc
# source .env


# ______________________________________________________________________________________________________

# versioning is not handled explicitly, but here are the employed versions while this is being written:
# npx hardhat --version
# 2.28.6

# npm -v
# 11.9.0

# nvm --version
# 0.40.4

# npx -v
# 11.9.0

# solc --version
# Version: 0.4.18+commit.9cf6e910.Linux.g++

# solc-select versions
# 0.4.18 (current, set by /root/.solc-select/global-version)

