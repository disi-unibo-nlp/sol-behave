#!/bin/bash

# Load environment variables

set -a
source .env
set +a

IMAGE_NAME=solbench
LLM_HOME_CACHE_DIR=./models/
CONTAINER_NAME=solcontract_container

# --------------------------------------------------
# Remove existing container if it exists
# --------------------------------------------------
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "Container ${CONTAINER_NAME} already exists. Removing it..."
    docker stop ${CONTAINER_NAME} 2>/dev/null || true
    docker rm ${CONTAINER_NAME} 2>/dev/null || true
fi

# --------------------------------------------------
# Run container
# --------------------------------------------------
#

docker run --rm \
  --gpus device=$DEVICE \
  --name $CONTAINER_NAME \
  --memory=50g \
  -v ${LLM_HOME_CACHE_DIR}:/models \
  -v ./local_models:/local_models \
  -v ./$ROOT_DIRECTORY:/workdir/$ROOT_DIRECTORY \
  -v ./scripts:/workdir/scripts/ \
  -v ./src:/workdir/src \
  --env-file .env \
  -it $IMAGE_NAME:latest