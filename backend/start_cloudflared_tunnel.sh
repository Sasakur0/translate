#!/usr/bin/env bash
set -euo pipefail

LOCAL_PORT="${1:-8000}"
LOCAL_HOST="${2:-127.0.0.1}"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not found. Install it first:"
  echo "  brew install cloudflared"
  exit 1
fi

echo "Starting quick tunnel for http://${LOCAL_HOST}:${LOCAL_PORT}"
echo "After startup, copy the https://*.trycloudflare.com URL."
echo "Then export PUBLIC_BASE_URL to that URL before starting backend."
echo

exec env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u no_proxy -u NO_PROXY \
  cloudflared tunnel --protocol http2 --edge-ip-version 4 --url "http://${LOCAL_HOST}:${LOCAL_PORT}"
