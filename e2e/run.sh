#!/bin/bash
# Build the image, start a throwaway instance, run the browser tests against it, clean up.
set -euo pipefail
cd "$(dirname "$0")/.."
NET=armadok-e2e-net
NAME=armadok-e2e
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker compose build >/dev/null
docker network create "$NET" >/dev/null
KEY=$(docker run --rm armadok:local python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
docker run -d --name "$NAME" --network "$NET" -e ARMADOK_COOKIE_SECURE=false -e ARMADOK_ENCRYPTION_KEY="$KEY" armadok:local >/dev/null
for _ in $(seq 1 60); do docker logs "$NAME" 2>&1 | grep -q "Install code" && break; sleep 1; done
CODE=$(docker logs "$NAME" 2>&1 | grep "Install code" | sed 's/.*): //' | tail -1)
docker run --rm --network "$NET" -v "$PWD/e2e":/work -w /work \
  -e BASE_URL="http://$NAME:8000" -e INSTALL_CODE="$CODE" \
  mcr.microsoft.com/playwright:v1.49.0-noble \
  sh -c "npm install --silent --no-audit --no-fund >/dev/null 2>&1 && npx playwright test"
