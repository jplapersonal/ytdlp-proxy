"""
ytdlp-proxy — Minimal YouTube audio URL extractor
Deployed on Render.com (free tier)

Endpoints:
  GET /audio-url?v=VIDEO_ID          → { url, duration_secs, title }
  GET /formats?v=VIDEO_ID            → list of available formats (debug)
  GET /health                         → { ok: true }
"""

import os
import subprocess
from flask import Flask, request, jsonify

app = Flask(__name__)

PROXY_SECRET = os.environ.get("PROXY_SECRET", "")
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")

COOKIES_FILE = "/tmp/yt_cookies.txt"


def check_secret():
    token = request.headers.get("X-Proxy-Secret", "") or request.args.get("secret", "")
    if PROXY_SECRET and token != PROXY_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    return None


def ensure_cookies():
    """Write cookies to disk once if available."""
    if YOUTUBE_COOKIES and not os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE, "w") as f:
            f.write(YOUTUBE_COOKIES)


def base_cmd():
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings"]
    if YOUTUBE_COOKIES:
        ensure_cookies()
        cmd += ["--cookies", COOKIES_FILE]
    return cmd


def validate_video_id(video_id):
    if not video_id or len(video_id) > 20:
        return False
    return all(c.isalnum() or c in '-_' for c in video_id)


@app.route("/health")
def health():
    return jsonify({"ok": True, "has_cookies": bool(YOUTUBE_COOKIES)})


@app.route("/formats")
def list_formats():
    """Debug endpoint — list all available formats for a video."""
    auth_error = check_secret()
    if auth_error:
        return auth_error

    video_id = request.args.get("v", "").strip()
    if not validate_video_id(video_id):
        return jsonify({"error": "Invalid video_id"}), 400

    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = base_cmd() + [
        "--list-formats",
        yt_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return jsonify({
            "stdout": result.stdout[:3000],
            "stderr": result.stderr[:500],
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timeout"}), 504


@app.route("/audio-url")
def audio_url():
    auth_error = check_secret()
    if auth_error:
        return auth_error

    video_id = request.args.get("v", "").strip()
    if not validate_video_id(video_id):
        return jsonify({"error": "Invalid video_id"}), 400

    yt_url = f"https://www.youtube.com/watch?v={video_id}"

    # Try formats in order of preference
    format_attempts = [
        "140",           # m4a 128kbps (most common)
        "bestaudio",     # best audio regardless of container
        "best",          # best overall (includes video, but URL works for AudD)
    ]

    for fmt in format_attempts:
        cmd = base_cmd() + [
            "--format", fmt,
            "--print", "%(title)s|||%(duration)s|||%(url)s",
            "--quiet",
            yt_url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            output = result.stdout.strip()
            if output and "|||" in output:
                parts = output.split("|||", 2)
                if len(parts) >= 3:
                    title, duration_str, stream_url = parts
                    duration = int(duration_str.strip()) if duration_str.strip().isdigit() else 0
                    return jsonify({
                        "title": title.strip(),
                        "duration_secs": duration,
                        "url": stream_url.strip(),
                        "format_used": fmt,
                    })
        except subprocess.TimeoutExpired:
            return jsonify({"error": f"yt-dlp timeout on format {fmt}"}), 504

    # All formats failed — return last stderr for diagnosis
    detail = result.stderr.strip()[:400] if 'result' in dir() else "no output"
    if "Sign in" in detail or "bot" in detail.lower():
        return jsonify({
            "error": "YouTube requires cookies",
            "detail": "Cookies seem invalid or expired — re-export from browser",
            "raw": detail,
        }), 403
    return jsonify({"error": "No suitable format found", "detail": detail}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
