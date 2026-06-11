"""
ytdlp-proxy — Minimal YouTube audio URL extractor
Deployed on Render.com (free tier)

Endpoints:
  GET /audio-url?v=VIDEO_ID          → { url, duration_secs, title }
  GET /health                         → { ok: true }
"""

import os
import subprocess
import tempfile
from flask import Flask, request, jsonify

app = Flask(__name__)

PROXY_SECRET = os.environ.get("PROXY_SECRET", "")
# Paste your YouTube cookies.txt content as this env var in Render
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")


def check_secret():
    token = request.headers.get("X-Proxy-Secret", "") or request.args.get("secret", "")
    if PROXY_SECRET and token != PROXY_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    return None


def build_ytdlp_cmd(video_id):
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--format", "140/bestaudio/best",
        "--print", "%(title)s|||%(duration)s|||%(url)s",
        "--no-warnings",
        "--quiet",
    ]
    # If cookies are provided as env var, write to a temp file
    if YOUTUBE_COOKIES:
        cookies_file = "/tmp/yt_cookies.txt"
        if not os.path.exists(cookies_file):
            with open(cookies_file, "w") as f:
                f.write(YOUTUBE_COOKIES)
        cmd += ["--cookies", cookies_file]

    cmd.append(yt_url)
    return cmd


@app.route("/health")
def health():
    return jsonify({"ok": True, "has_cookies": bool(YOUTUBE_COOKIES)})


@app.route("/audio-url")
def audio_url():
    auth_error = check_secret()
    if auth_error:
        return auth_error

    video_id = request.args.get("v", "").strip()
    if not video_id or len(video_id) > 20:
        return jsonify({"error": "Invalid video_id"}), 400
    # Basic sanitization
    if not all(c.isalnum() or c in '-_' for c in video_id):
        return jsonify({"error": "Invalid video_id characters"}), 400

    try:
        result = subprocess.run(
            build_ytdlp_cmd(video_id),
            capture_output=True,
            text=True,
            timeout=45,
        )

        output = result.stdout.strip()
        if not output or "|||" not in output:
            detail = result.stderr.strip()[:300]
            # If cookies needed, give clear hint
            if "Sign in" in detail or "bot" in detail.lower():
                return jsonify({
                    "error": "YouTube requires cookies",
                    "detail": "Set YOUTUBE_COOKIES env var in Render with your cookies.txt content",
                    "raw": detail,
                }), 403
            return jsonify({"error": "yt-dlp failed", "detail": detail}), 500

        parts = output.split("|||", 2)
        if len(parts) < 3:
            return jsonify({"error": "Unexpected output", "detail": output[:200]}), 500

        title, duration_str, stream_url = parts
        duration = int(duration_str) if duration_str.strip().isdigit() else 0

        return jsonify({
            "title": title.strip(),
            "duration_secs": duration,
            "url": stream_url.strip(),
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "yt-dlp timeout (>45s)"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
