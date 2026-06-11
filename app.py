"""
ytdlp-proxy — Minimal YouTube audio URL extractor
Deployed on Render.com (free tier)

Endpoints:
  GET /audio-url?v=VIDEO_ID          → { url, duration_secs, title }
  GET /health                         → { ok: true }
"""

import os
import subprocess
import json
from flask import Flask, request, jsonify

app = Flask(__name__)

# Simple secret to avoid abuse (set as env var in Render)
PROXY_SECRET = os.environ.get("PROXY_SECRET", "")


def check_secret():
    token = request.headers.get("X-Proxy-Secret", "") or request.args.get("secret", "")
    if PROXY_SECRET and token != PROXY_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/audio-url")
def audio_url():
    auth_error = check_secret()
    if auth_error:
        return auth_error

    video_id = request.args.get("v", "").strip()
    if not video_id or not video_id.isalnum() or len(video_id) > 20:
        return jsonify({"error": "Invalid video_id"}), 400

    yt_url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        # Get JSON info (title + duration) and best audio URL
        info_result = subprocess.run(
            [
                "yt-dlp",
                "--no-playlist",
                "--format", "bestaudio[ext=m4a]/bestaudio/best",
                "--print", "%(title)s|||%(duration)s|||%(url)s",
                "--no-warnings",
                "--quiet",
                yt_url,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = info_result.stdout.strip()
        if not output or "|||" not in output:
            return jsonify({"error": "yt-dlp failed", "detail": info_result.stderr[:200]}), 500

        parts = output.split("|||", 2)
        if len(parts) < 3:
            return jsonify({"error": "Unexpected yt-dlp output", "detail": output[:200]}), 500

        title, duration_str, stream_url = parts
        duration = int(duration_str) if duration_str.isdigit() else 0

        return jsonify({
            "title": title,
            "duration_secs": duration,
            "url": stream_url,
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "yt-dlp timeout"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
