# Tunnel + Doubao Integration

This project now supports a stable public-media layer for `doubao_asr + YouTube`.

## What was added

- Signed temporary public media endpoint:
  - `GET /api/public-media/{file_id}?expires=...&sign=...`
- Backend workflow for `engine=doubao_asr` and YouTube input:
  1. Download source media locally
  2. Convert to `wav` (16k, mono)
  3. Publish a temporary signed URL
  4. Submit that URL to Doubao ASR

This avoids sending short-lived `yt-dlp -g` links directly to Doubao.

## Environment variables

Configure these before starting backend:

- `PUBLIC_BASE_URL`:
  - Public base URL reachable by Doubao
  - Example: `https://abc123.trycloudflare.com`
- `PUBLIC_MEDIA_DIR` (optional):
  - Temp directory for published audio files
  - Default: `/tmp/translate-public-media`
- `PUBLIC_MEDIA_TTL_SECONDS` (optional):
  - Signed URL lifetime
  - Default: `7200`
- `PUBLIC_MEDIA_SECRET` (recommended):
  - HMAC secret for signed URLs

## Quick start with cloudflared

1) Start backend (local):

```bash
cd /Users/newin/Desktop/workspace/translate
export PUBLIC_MEDIA_SECRET="change-this-secret"
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

2) Start tunnel in another terminal:

```bash
cd /Users/newin/Desktop/workspace/translate/backend
bash ./start_cloudflared_tunnel.sh 8000 127.0.0.1
```

3) Copy generated public URL, then restart backend with:

```bash
export PUBLIC_BASE_URL="https://your-trycloudflare-domain.trycloudflare.com"
export PUBLIC_MEDIA_SECRET="change-this-secret"
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

Important: `PUBLIC_BASE_URL` must be the tunnel URL so Doubao can fetch `/api/public-media/...`.

## Fixed-domain tunnel (recommended)

Use `cloudflared-tunnel.example.yml` as a template, then run:

```bash
cloudflared tunnel --config /path/to/your.yml run
```

A fixed domain is more stable than a quick temporary domain.

## Validation checklist

1) Health check:

```bash
curl -s "http://127.0.0.1:8000/health"
```

2) Create Doubao task with YouTube URL:

```bash
curl -s -X POST "http://127.0.0.1:8000/api/generate" \
  -H "Content-Type: application/json" \
  -d '{
    "video_url":"https://www.youtube.com/watch?v=PfRnJDGnV_Y",
    "params":{
      "engine":"doubao_asr",
      "sourceLanguage":"auto",
      "targetLanguage":"zh",
      "type":"summary"
    }
  }'
```

3) Poll status:

```bash
curl -s "http://127.0.0.1:8000/api/generate/<taskId>"
```

If tunnel and `PUBLIC_BASE_URL` are correct, `45000006 Invalid audio URI` should be significantly reduced.
