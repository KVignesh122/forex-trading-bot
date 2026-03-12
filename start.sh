#!/bin/bash
# Start the Forex Trading Bot
set -e

cd "$(dirname "$0")"

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    echo "Installing dependencies..."
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

echo ""
echo "========================================="
echo "  FOREX TRADING BOT"
echo "========================================="
echo ""
echo "  Dashboard: http://localhost:8080"
echo "  Press Ctrl+C to stop"
echo ""
echo "========================================="
echo ""

python main.py
