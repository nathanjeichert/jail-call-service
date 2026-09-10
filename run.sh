#!/bin/bash
# Start both backend and frontend together.
# Run from the jail-call-service/ directory.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Jail Call Service ==="
echo ""

# ── Pre-flight checks ──
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo "Warning: No .env file found. Copy .env.example and fill in API keys."
  echo ""
fi

# Kill anything already on our ports (avoids EADDRINUSE)
cleanup_ports() {
  for port in 8000 3000; do
    lsof -ti :$port 2>/dev/null | xargs kill -9 2>/dev/null || true
  done
}

cleanup_ports
sleep 1

# ── Start backend ──
echo "Starting backend on http://localhost:8000 ..."
cd "$SCRIPT_DIR"
WATCHFILES_FORCE_POLLING=1 uvicorn backend.server:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!

# Give backend a moment to start
sleep 2

# ── Start frontend ──
echo "Starting frontend on http://localhost:3000 ..."
cd "$SCRIPT_DIR/frontend"

# Install deps if needed
if [ ! -d "node_modules" ]; then
  echo "Installing frontend dependencies..."
  npm install
fi

npm run dev &
FRONTEND_PID=$!

echo ""
echo "Running! Open http://localhost:3000 in your browser."
echo "Press Ctrl+C to stop both servers."
echo ""

# ── Graceful shutdown ──
shutdown() {
  echo ""
  echo "Shutting down..."
  kill $FRONTEND_PID 2>/dev/null
  kill $BACKEND_PID 2>/dev/null
  # Give them a moment, then force-kill
  sleep 2
  kill -9 $FRONTEND_PID 2>/dev/null || true
  kill -9 $BACKEND_PID 2>/dev/null || true
  echo "Done."
  exit 0
}

trap shutdown INT TERM
wait
