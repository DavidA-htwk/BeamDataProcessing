#!/usr/bin/env bash
# Creates a virtual environment and installs all dependencies.
# Run once after cloning:  bash install.sh

set -e

echo "=== BeamDataProcessing Setup ==="

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Please install Python 3.10+."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
else
    echo "Virtual environment already exists, skipping creation."
fi

echo "Activating and installing dependencies..."
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "=== Setup complete ==="
echo "To run the application:"
echo "  source .venv/bin/activate"
echo "  python Data_handling.py"
