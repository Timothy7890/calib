#!/bin/bash
# Install systemctl services for robot-twoeyes
# Usage: sudo bash services/install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing services..."

sudo cp "$SCRIPT_DIR/yolo-detect.service" /etc/systemd/system/
sudo cp "$SCRIPT_DIR/robot-twoeyes.service" /etc/systemd/system/
sudo cp "$SCRIPT_DIR/robot-twoeyes-web.service" /etc/systemd/system/
sudo cp "$SCRIPT_DIR/crestereo.service" /etc/systemd/system/

sudo systemctl daemon-reload

echo "Enabling services..."
sudo systemctl enable yolo-detect.service
sudo systemctl enable robot-twoeyes.service
sudo systemctl enable robot-twoeyes-web.service
sudo systemctl enable crestereo.service

echo ""
echo "Done! Services installed. Commands:"
echo ""
echo "  Start all:   sudo systemctl start yolo-detect robot-twoeyes robot-twoeyes-web crestereo"
echo "  Stop all:    sudo systemctl stop yolo-detect robot-twoeyes robot-twoeyes-web crestereo"
echo "  Check status: systemctl status yolo-detect robot-twoeyes robot-twoeyes-web crestereo"
echo "  View logs:   journalctl -u yolo-detect -f"
echo "               journalctl -u robot-twoeyes -f"
echo "               journalctl -u robot-twoeyes-web -f"
echo "               journalctl -u crestereo -f"
