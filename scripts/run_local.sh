#!/usr/bin/env bash
# Runs the whole stack locally without Docker or Kubernetes (handy for development and tests).
#   storefront :8001   cart :8002   inventory :8003   payments :8004   sidekick/dashboard :8000
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a

export CART_URL=http://localhost:8002 INVENTORY_URL=http://localhost:8003 PAYMENTS_URL=http://localhost:8004
export STOREFRONT_URL=http://localhost:8001 SIDEKICK_URL=http://localhost:8000

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null; }
trap cleanup EXIT INT TERM

for pair in storefront:8001 cart:8002 inventory:8003 payments:8004; do
  role=${pair%%:*}; port=${pair##*:}
  ROLE=$role python3 -m uvicorn shop.app:app --host 0.0.0.0 --port "$port" --log-level warning &
  pids+=($!)
done
python3 -m uvicorn sidekick.app:app --host 0.0.0.0 --port 8000 --log-level warning &
pids+=($!)

echo "SahajSale running: dashboard http://localhost:8000   store http://localhost:8001   (Ctrl+C to stop)"
wait
