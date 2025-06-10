#!/bin/bash

# Kill any existing processes on ports 8000 and 4040 (cloudflared metrics)
pkill -f "uvicorn.*:8000" || true
pkill -f "cloudflared.*tunnel" || true

# Start FastAPI server in the background
echo "Starting FastAPI server..."
cd /Users/louisleng/Downloads/ai-hedge-fund-with-gui
PYTHONPATH=$PYTHONPATH:. poetry run uvicorn app.backend.main:app --host 0.0.0.0 --port 8000 &
FASTAPI_PID=$!

# Wait a few seconds for FastAPI to start
sleep 5

# Start Cloudflare tunnel in the background
echo "Starting Cloudflare tunnel..."
cloudflared tunnel --config ~/.cloudflared/config.yml run ai-hedge-fund-backend &
TUNNEL_PID=$!

# Function to handle script termination
cleanup() {
    echo "Shutting down services..."
    kill $FASTAPI_PID
    kill $TUNNEL_PID
    exit 0
}

# Register the cleanup function for when script is terminated
trap cleanup SIGINT SIGTERM

# Keep script running and show logs
echo "Services started. Press Ctrl+C to stop."
tail -f /dev/null 