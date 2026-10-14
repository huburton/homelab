#!/usr/bin/env bash
# Validate every stack with the placeholder values of .env.example (no container is started).
set -euo pipefail
cd "$(dirname "$0")/.."
status=0
for f in stacks/*/compose.yml; do
    if docker compose --env-file .env.example -f "$f" config -q; then
        echo "ok   $f"
    else
        echo "FAIL $f"
        status=1
    fi
done
exit "$status"
