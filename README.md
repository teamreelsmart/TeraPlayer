# TeraPlayer Telegram Bot (MongoDB + Render Ready)

This project provides a Telegram bot + mini web app that converts TeraBox links into stream and download flows.

## What is updated
- Full bot UX converted to English.
- Database switched from SQLite to MongoDB (`pymongo`).
- Render-ready port handling (`PORT` fallback supported).
- Health endpoint added: `GET /health`.

## Core Features
- First-time `/start` detection with log channel notification.
- Force-subscription check before allowing usage.
- Free-user verification flow via shortener link.
- Anti-bypass check: early verification within 3 minutes issues warning and regenerates verification link.
- Access window after verification (default 4 hours).
- Premium/free routing:
  - Free: `stream_url` + `download_link`
  - Premium: `fast_stream_url` + `fast_download_link`
- Mini app player route: `/mini/{token}`
- Download gate route: `/d/{token}`
- Admin panel (`/admin`) with settings buttons:
  - add/remove premium user
  - shortener URL and API key
  - verify tutorial link
  - premium QR image
  - force-sub channel ID + username
  - log channel ID
  - multiple TeraBox API keys
  - supported domains

## Python version (important for Render)
- Use **Python 3.12.x** for this project.
- `aiogram==3.13.1` depends on `pydantic-core`; on Python 3.14 Render may try to build from source and fail with Rust/cargo filesystem errors.
- This repo now includes both `runtime.txt` and `render.yaml` to pin Python 3.12.12.

## Environment
Create `.env` from `.env.example` and fill values:
- `BOT_TOKEN`
- `WEB_BASE_URL` (your Render/Koyeb base domain)
- `PORT` (Render uses this automatically)
- `MONGODB_URI`
- `MONGODB_DB`
- `ADMIN_IDS` (comma-separated Telegram user IDs allowed to use `/admin`)

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

## Render deployment notes
- Start command: `python app.py`
- Make sure service uses Python `3.12.12` (already pinned in `runtime.txt` / `render.yaml`).
- Add all env vars in Render dashboard.
- Ensure `WEB_BASE_URL` is your public Render URL.
- Use MongoDB Atlas URI in `MONGODB_URI`.
- Keep bot running as a web service (long-running process).

## Quick admin setup after deploy
Set `ADMIN_IDS` in env first (example: `123456789,987654321`), then use `/admin` and configure:
1. `terabox_api_keys`
2. `force_sub_channel_id` (+ username for join button)
3. `log_channel_id`
4. shortener URL/API key (optional)
5. tutorial and premium QR (optional)

## Troubleshooting
- If you see `metadata-generation-failed` for `pydantic-core` on Render, your service is likely building with Python 3.14. Re-deploy after ensuring Python is pinned to 3.12.12.
