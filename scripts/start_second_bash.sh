#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# Open a second interactive shell inside the already-running
# solcontract_container, so you can poke at stats / fix things while the
# differential fuzzing (or any long job) keeps running in the first shell.
#
# This does NOT start a new container — it execs into the existing one, so
# it shares the same filesystem, env, and running processes.
#
# Usage:
#   ./start_second_bash.sh            # bash with nvm/node 24 sourced
#   ./start_second_bash.sh <cmd...>   # run a one-off command, then exit
# ----------------------------------------------------------------------------
set -euo pipefail

CONTAINER="${CONTAINER:-solcontract_container}"

# Verify the container is actually running
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "ERROR: container '$CONTAINER' is not running."
  echo "Running containers:"
  docker ps --format '  {{.Names}}'
  exit 1
fi

# Source nvm + Node 24 (Hardhat needs >=18) inside the interactive shell so
# any hardhat/npx commands you run by hand work the same as the pipeline's.
INIT='source ~/.nvm/nvm.sh 2>/dev/null; nvm use 24 >/dev/null 2>&1; cd /workdir 2>/dev/null; exec bash'

if [[ $# -gt 0 ]]; then
  # One-off command mode: run it (with node sourced) and exit
  docker exec -it "$CONTAINER" bash -lc \
    "source ~/.nvm/nvm.sh 2>/dev/null; nvm use 24 >/dev/null 2>&1; cd /workdir 2>/dev/null; $*"
else
  # Interactive shell mode
  docker exec -it "$CONTAINER" bash -lc "$INIT"
fi