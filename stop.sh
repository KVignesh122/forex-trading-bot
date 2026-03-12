#!/bin/bash
# Stop the Forex Trading Bot gracefully
cd "$(dirname "$0")"

PID=$(lsof -ti:8080 2>/dev/null)
if [ -n "$PID" ]; then
    echo "Stopping bot (PID: $PID)..."
    kill "$PID"
    echo "Bot stopped."
else
    echo "Bot is not running."
fi
