#!/usr/bin/env bash
set -e

SERVICE_NAME="pihole-mcp"
PROJECT_DIR="/root/pihole-mcp-server"   # adjust if your project lives elsewhere
START_SCRIPT="$PROJECT_DIR/start_openapi.sh"

# 1. Ensure the start script is executable
chmod +x "$START_SCRIPT"

# 2. Create systemd service unit
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Pi-hole MCP OpenAPI Wrapper
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
ExecStart=$START_SCRIPT
Restart=on-failure
Environment=DISABLE_MAIN_SIGNAL_HANDLER=1

[Install]
WantedBy=multi-user.target
EOF

# 3. Reload systemd, enable and start
systemctl daemon-reexec
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo "Service $SERVICE_NAME installed and started."
echo "Check status with: systemctl status $SERVICE_NAME"
