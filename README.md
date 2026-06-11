# ytdlp-proxy

Minimal Python proxy that extracts YouTube audio stream URLs using yt-dlp.
Used by the ReloadTrack bot to enable audio fingerprinting of YouTube mixes.

## Endpoint

```
GET /audio-url?v=VIDEO_ID
Header: X-Proxy-Secret: <your_secret>

→ { "title": "...", "duration_secs": 3596, "url": "https://..." }
```

## Deploy on Render.com (free)

1. Push this folder to a GitHub repo
2. Go to render.com → New → Web Service
3. Connect your repo
4. Settings:
   - **Runtime**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn app:app --bind 0.0.0.0:$PORT --timeout 60`
5. Environment Variables:
   - `PROXY_SECRET` = any random string (e.g. `rt2026proxy`)
6. Deploy → copy the URL (e.g. `https://ytdlp-proxy.onrender.com`)

## Configure in Cloudflare

Add two environment variables to Cloudflare Pages → reloadtrack-app → Settings → Variables:
- `YTDLP_PROXY_URL` = `https://ytdlp-proxy.onrender.com`
- `YTDLP_PROXY_SECRET` = same value as `PROXY_SECRET` above
