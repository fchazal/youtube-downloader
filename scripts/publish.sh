#!/usr/bin/env bash
# Build and push the image to a container registry.
# Usage: ./scripts/publish.sh [registry/org]
# Example: ./scripts/publish.sh ghcr.io/your-username
set -euo pipefail

IMAGE="${1:-ghcr.io/your-username}/yt-dlp-api:latest"

echo "Building ${IMAGE} …"
docker build -t "${IMAGE}" .

echo "Pushing ${IMAGE} …"
docker push "${IMAGE}"

echo "Done."
