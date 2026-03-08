#!/bin/bash
set -e

CONFIG_PATH="${PIXLVAULT_CONFIG:-/data/config/server-config.json}"
IMAGE_ROOT="${PIXLVAULT_IMAGE_ROOT:-/data/images}"
HOST="${PIXLVAULT_HOST:-0.0.0.0}"
PORT="${PIXLVAULT_PORT:-9537}"

mkdir -p "$(dirname "$CONFIG_PATH")" "$IMAGE_ROOT"

# Write a Docker-appropriate default config on first run.
# If the config already exists it is left untouched so user edits survive restarts.
if [ ! -f "$CONFIG_PATH" ]; then
    echo "Creating default server config at $CONFIG_PATH"
    cat > "$CONFIG_PATH" <<EOF
{
  "host": "$HOST",
  "port": $PORT,
  "log_level": "info",
  "log_file": null,
  "require_ssl": false,
  "cookie_samesite": "Lax",
  "cookie_secure": false,
  "image_root": "$IMAGE_ROOT",
  "default_device": "auto",
  "min_free_disk_gb": 1.0,
  "min_free_vram_mb": 1024.0,
  "cors_origins": [],
  "watch_folders": []
}
EOF
fi

exec python -m pixlvault.app --server-config "$CONFIG_PATH" "$@"
