#!/bin/bash
# Start both backend and frontend together.
# Run from the jail-call-service/ directory.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Jail Call Service ==="
echo ""

# Check for .env
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo "Warning: No .env file found. Copy .env.example and fill in API keys."
  echo ""
fi

# Start backend
echo "Starting backend on http://localhost:8000 ..."
cd "$SCRIPT_DIR"
uvicorn backend.server:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!

# Give backend a moment to start
sleep 2

# Start frontend
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

# Wait for both
wait_for_signal() {
  trap 'kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0' INT TERM
  wait
}
wait_for_signal
