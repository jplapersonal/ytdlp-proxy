"""
ytdlp-proxy LOCAL — Full YouTube audio fingerprinting pipeline
Runs on your Mac (residential IP). Exposed via Cloudflare Tunnel.

Endpoints:
  POST /fingerprint-async          Body: {video_id, chat_id, bot_token}
    → Starts fingerprinting in background, sends result to Telegram
  GET  /audio-url?v=VIDEO_ID       → { url, duration_secs, title }
  GET  /health                      → { ok: true }
"""

import os
import sys
import asyncio
import base64
import hashlib
import glob
import json
import re
import time
import tempfile
import threading
import struct
import subprocess
import concurrent.futures
import requests
from shazamio import Shazam
from flask import Flask, request, jsonify

app = Flask(__name__)


@app.after_request
def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def _options(path):
    from flask import Response
    return Response(status=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    })

PROXY_SECRET  = os.environ.get("PROXY_SECRET", "rt2026proxy")
APP_WORKER_URL = "https://reloadtrack-app.pages.dev"

def _register_tunnel_url(url):
    """Push tunnel URL to D1 via API so dashboard can auto-discover it."""
    try:
        r = requests.post(
            f"{APP_WORKER_URL}/api/proxy-url",
            json={"url": url, "secret": PROXY_SECRET},
            timeout=10
        )
        print(f"[tunnel] Registered URL: {url} → {r.status_code}", flush=True)
    except Exception as e:
        print(f"[tunnel] Could not register URL: {e}", flush=True)
AUDD_TOKEN    = os.environ.get("AUDD_TOKEN", "761bb9aaa3e34dd0203a5ce915842dd8")
APP_BASE_URL  = os.environ.get("APP_BASE_URL", "https://reloadtrack-app.pages.dev")
# Flask + shazamio run on Python 3.9 (audioop required by pydub)
# yt-dlp subprocesses use Python 3.13 if available (better extractor support)
_PY313 = "/opt/homebrew/opt/python@3.13/bin/python3.13"
PYTHON = sys.executable
YT_DLP = [_PY313 if os.path.exists(_PY313) else PYTHON, "-m", "yt_dlp"]

SAMPLE_INTERVAL = int(os.environ.get("SAMPLE_INTERVAL", "180"))  # every 3 min by default
SEGMENT_SECS    = 15    # 15-second clips
MAX_WORKERS     = 3     # parallel Shazam calls (conservative to avoid rate limit)
CACHE_DIR       = "/tmp/yt_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

# Shazamio instance (direct Shazam API — no API key needed)
_shazam = Shazam()


def check_secret():
    token = request.headers.get("X-Proxy-Secret", "") or request.args.get("secret", "")
    if token != PROXY_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/disk-check")
def disk_check():
    auth_error = check_secret()
    if auth_error: return auth_error
    import subprocess as _sp
    volumes = _sp.run(["ls", "/Volumes/"], capture_output=True, text=True).stdout.strip()
    musica_ok = os.path.isdir("/Volumes/Musica")
    reloadtrack_ok = os.path.isdir("/Volumes/Musica/ReloadTrack")
    try:
        test_path = "/Volumes/Musica/ReloadTrack/.write_test"
        with open(test_path, "w") as f: f.write("ok")
        os.remove(test_path)
        write_ok = True
    except Exception as e:
        write_ok = str(e)
    return jsonify({
        "volumes": volumes,
        "musica_exists": musica_ok,
        "reloadtrack_exists": reloadtrack_ok,
        "write_ok": write_ok,
        "pid": os.getpid(),
        "user": _sp.run(["whoami"], capture_output=True, text=True).stdout.strip()
    })

def validate_video_id(vid):
    return vid and len(vid) <= 20 and all(c.isalnum() or c in '-_' for c in vid)


def send_telegram(bot_token, chat_id, text, parse_mode="Markdown"):
    """Send a message directly to Telegram."""
    print(f"[tg send] to={chat_id} token={bot_token[:5] if bot_token else 'None'} text={text[:20]}", flush=True)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Telegram API Error: {r.status_code} {r.text}", flush=True)
    except Exception as e:
        print(f"Telegram send error: {e}", flush=True)


def get_video_info(video_id):
    """Get title and duration for a YouTube video_id via yt-dlp."""
    r = subprocess.run(
        YT_DLP + ["--no-playlist", "--no-warnings", "--quiet",
                  "--extractor-args", "youtube:player_client=android",
                  "--print", "%(title)s|||%(duration)s",
                  f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True, text=True, timeout=30,
    )
    out = r.stdout.strip()
    if out and "|||" in out:
        parts = out.split("|||", 1)
        title = parts[0].strip()
        duration = int(parts[1].strip()) if parts[1].strip().isdigit() else 3600
        return title, duration
    return "YouTube", 3600


def get_url_info(url):
    """Get title and duration for any URL via yt-dlp (generic, no YouTube-specific args)."""
    r = subprocess.run(
        YT_DLP + ["--no-playlist", "--no-warnings", "--quiet",
                  "--print", "%(title)s|||%(duration)s",
                  url],
        capture_output=True, text=True, timeout=60,
    )
    out = r.stdout.strip()
    if out and "|||" in out:
        parts = out.split("|||", 1)
        title = parts[0].strip()
        duration = int(parts[1].strip()) if parts[1].strip().isdigit() else 3600
        return title, duration
    return "Sesión", 3600


def download_video(video_id):
    """Download YouTube video to cache using android client. Returns local file path."""
    existing = [f for f in glob.glob(os.path.join(CACHE_DIR, f"{video_id}.*")) if "_test" not in f]
    if existing:
        print(f"Cache hit: {existing[0]}")
        return existing[0]

    print(f"Downloading {video_id} (android client)...")
    out_tmpl = os.path.join(CACHE_DIR, f"{video_id}.%(ext)s")
    r = subprocess.run(
        YT_DLP + ["--no-playlist", "--no-warnings", "--quiet",
                  "--extractor-args", "youtube:player_client=android",
                  "--format", "bestaudio/best",
                  "-o", out_tmpl,
                  f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp download failed: {r.stderr.strip()[:300]}")

    files = [f for f in glob.glob(os.path.join(CACHE_DIR, f"{video_id}.*")) if "_test" not in f]
    if not files:
        raise RuntimeError("yt-dlp ran but no output file found")
    print(f"Downloaded: {files[0]} ({os.path.getsize(files[0])//1024//1024} MB)")
    return files[0]


def download_url(url, cache_id):
    """Download any URL to cache via yt-dlp (generic). Returns local file path."""
    existing = [f for f in glob.glob(os.path.join(CACHE_DIR, f"{cache_id}.*")) if "_test" not in f]
    if existing:
        print(f"Cache hit: {existing[0]}")
        return existing[0]

    print(f"Downloading {cache_id} from {url} ...")
    out_tmpl = os.path.join(CACHE_DIR, f"{cache_id}.%(ext)s")

    # Instagram requires browser cookies (session auth) — pass Chrome cookies
    extra_args = []
    if "instagram.com" in url:
        extra_args = ["--cookies-from-browser", "chrome"]

    r = subprocess.run(
        YT_DLP + ["--no-playlist", "--no-warnings", "--quiet",
                  "--format", "bestaudio/best",
                  "-o", out_tmpl] + extra_args + [url],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp download failed: {r.stderr.strip()[:300]}")

    files = [f for f in glob.glob(os.path.join(CACHE_DIR, f"{cache_id}.*")) if "_test" not in f]
    if not files:
        raise RuntimeError("yt-dlp ran but no output file found")
    print(f"Downloaded: {files[0]} ({os.path.getsize(files[0])//1024//1024} MB)")
    return files[0]


def extract_segment_local(local_file, start_sec):
    """Extract SEGMENT_SECS of audio from a local file as MP3 bytes (for AudD)."""
    cmd = ["ffmpeg", "-v", "quiet",
           "-ss", str(start_sec),
           "-i", local_file,
           "-t", str(SEGMENT_SECS),
           "-vn", "-f", "mp3", "-q:a", "5",
           "pipe:1"]
    r = subprocess.run(cmd, capture_output=True, timeout=30)
    if r.returncode == 0 and len(r.stdout) > 1000:
        return r.stdout
    return None


def shazam_recognize(local_file, start_sec):
    """Recognize via shazamio (direct Shazam API, no key needed). Returns {artist, title, t} or None."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp = f.name
    try:
        # Extract 15s MP3 segment to temp file
        cmd = ["ffmpeg", "-v", "quiet",
               "-ss", str(start_sec), "-i", local_file,
               "-t", str(SEGMENT_SECS), "-vn", "-f", "mp3", "-q:a", "5",
               tmp, "-y"]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode != 0 or os.path.getsize(tmp) < 1000:
            return None
        # asyncio.run() creates a new event loop per thread — safe in ThreadPoolExecutor
        result = asyncio.run(_shazam.recognize(tmp))
        track = result.get("track")
        if track:
            return {
                "artist": track.get("subtitle", ""),
                "title": track.get("title", ""),
                "t": start_sec,
                "source": "shazam",
            }
    except Exception as e:
        print(f"Shazam error t={start_sec}: {e}", flush=True)
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    return None


def audd_recognize_bytes(mp3_bytes, start_sec):
    """POST mp3 bytes to AudD. Returns {artist, title, t} or None."""
    try:
        resp = requests.post(
            "https://api.audd.io/",
            data={"api_token": AUDD_TOKEN},
            files={"file": ("seg.mp3", mp3_bytes, "audio/mpeg")},
            timeout=12,
        )
        data = resp.json()
        if data.get("status") == "success" and data.get("result"):
            r = data["result"]
            return {"artist": r.get("artist", ""), "title": r.get("title", ""), "t": start_sec, "source": "audd"}
        if data.get("error", {}).get("error_code") == 902:
            raise RuntimeError("AUDD_LIMIT_REACHED")
    except RuntimeError:
        raise
    except Exception:
        pass
    return None


def fingerprint_worker(video_id, chat_id, bot_token):
    """Background thread for YouTube video_id fingerprinting."""
    try:
        yt_url = f"https://www.youtube.com/watch?v={video_id}"
        title, duration = get_video_info(video_id)
        local_file = download_video(video_id)
        _run_fingerprint(video_id, local_file, duration, chat_id, bot_token, yt_url, title)
    except Exception as e:
        send_telegram(bot_token, chat_id, f"❌ Error: {str(e)[:200]}")


def fingerprint_worker_url(url, cache_id, chat_id, bot_token):
    """Background thread for generic URL fingerprinting (iVoox, etc.)."""
    try:
        title, duration = get_url_info(url)
        local_file = download_url(url, cache_id)
        _run_fingerprint(cache_id, local_file, duration, chat_id, bot_token, url, title)
    except Exception as e:
        send_telegram(bot_token, chat_id, f"❌ Error: {str(e)[:200]}")


def _run_fingerprint(label, local_file, duration, chat_id, bot_token, source_url, title):
    """Shared fingerprint logic used by both YouTube and generic URL workers."""
    try:
        # Step 3: build timestamp list
        samples = list(range(60, max(duration - 60, 61), SAMPLE_INTERVAL))
        print(f"[{label}] {len(samples)} samples @ every {SAMPLE_INTERVAL}s", flush=True)

        # Step 4: extract segments + Shazam/AudD in parallel
        def process_one(t):
            # Shazam first (better coverage for electronic), AudD as fallback
            result = shazam_recognize(local_file, t)
            if result:
                return result
            # AudD fallback (needs MP3)
            mp3 = extract_segment_local(local_file, t)
            if not mp3:
                return None
            return audd_recognize_bytes(mp3, t)

        tracks_raw = []
        matched = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(process_one, t): t for t in samples}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    result = fut.result()
                    if result:
                        tracks_raw.append(result)
                        matched += 1
                        print(f"[{label}] ✅ t={result['t']}s: {result['artist']} — {result['title']}", flush=True)
                        if matched in (1, 5, 10, 15, 20, 25, 30, 40, 50, 60):
                            send_telegram(bot_token, chat_id, f"🔍 *Avance*: Ya he detectado {matched} tracks de la sesión. Sigo escuchando...")
                except RuntimeError as e:
                    if "AUDD_LIMIT" in str(e):
                        send_telegram(bot_token, chat_id,
                            "⚠️ Límite mensual de AudD alcanzado. Inténtalo el mes que viene.")
                        return
        print(f"[{label}] Resultado: {matched}/{len(samples)} matches", flush=True)

        # Step 5: sort + deduplicate
        seen = set()
        tracks = []
        for r in sorted(tracks_raw, key=lambda x: x["t"]):
            key = f"{r['artist']}|{r['title']}".lower()
            if key not in seen:
                seen.add(key)
                tracks.append(r)

        # Step 6: send result to Telegram
        if not tracks:
            send_telegram(bot_token, chat_id,
                f"🤔 No pude identificar tracks en *{title}* ({matched}/{len(samples)} muestras)."
                "\n\n¿Es un mix underground/electrónico? AudD no cubre bien esos géneros."
                "\n💡 Busca la tracklist en YouTube (descripción/comentarios) y pégala aquí.")
            return

        lines = [f"🎵 *{title}*", f"_{len(tracks)}/{len(samples)} tracks identificados_\n"]
        for i, t in enumerate(tracks, 1):
            lines.append(f"{i}. {t['artist']} — {t['title']}")
        msg = "\n".join(lines)

        # Push tracks to D1 via Worker endpoint, get back a queue ID
        import json as _json
        tracks_payload = [{"artist": t["artist"], "title": t["title"]} for t in tracks]
        queue_id = None
        try:
            r = requests.post(
                f"{APP_BASE_URL}/api/bot-queue?secret={PROXY_SECRET}",
                json={"tracks": tracks_payload},
                timeout=10,
            )
            if r.ok:
                queue_id = r.json().get("id")
                print(f"[{label}] Tracks stored in D1 queue: {queue_id}", flush=True)
        except Exception as e:
            print(f"[{label}] D1 queue error: {e}", flush=True)

        # Step 6b: extract mix transitions from ordered (non-deduplicated) results
        # Use tracks_raw sorted by timestamp to detect consecutive pairs
        try:
            ordered_raw = sorted(tracks_raw, key=lambda x: x["t"])
            transitions = []
            # Extract DJ name from session title (e.g. "Zona AfterHours - DJ Victor Belenguer")
            import re as _re
            dj_match = _re.search(r'(?:DJ|Dj|dj)[.\s]+([A-Za-záéíóúÁÉÍÓÚüÜñÑ\s]+?)(?:\s*[-–|]|$)', title)
            dj_name = dj_match.group(1).strip() if dj_match else ''
            for i in range(len(ordered_raw) - 1):
                a = ordered_raw[i]
                b = ordered_raw[i + 1]
                # Skip same track
                if a["artist"] == b["artist"] and a["title"] == b["title"]:
                    continue
                time_diff = b["t"] - a["t"]
                # STRICT: only record if B is in the IMMEDIATELY next sample slot.
                # If diff > SAMPLE_INTERVAL, there was an undetected track in between
                # → they were NOT directly mixed together.
                if time_diff == SAMPLE_INTERVAL:
                    transitions.append({
                        "a": {"artist": a["artist"], "title": a["title"]},
                        "b": {"artist": b["artist"], "title": b["title"]},
                        "dj_name": dj_name,
                        "source_url": source_url,
                    })
            if transitions:
                r2 = requests.post(
                    f"{APP_BASE_URL}/api/mix-transitions?secret={PROXY_SECRET}",
                    json={"transitions": transitions, "dj_name": dj_name, "source_url": source_url},
                    timeout=10,
                )
                print(f"[{label}] Mix transitions stored: {r2.json().get('stored', '?')}", flush=True)
        except Exception as e:
            print(f"[{label}] Mix transitions error: {e}", flush=True)

        # Build button: callback if queued (silent), URL fallback otherwise
        if queue_id:
            button_wl = {"text": "💾 Añadir a Wishlist", "callback_data": f"wl:{queue_id}"}
            button_tag = {"text": "🏷️ Con Tag...", "callback_data": f"wltag:{queue_id}"}
            keyboard = [[button_wl, button_tag]]
        else:
            import base64 as _b64, urllib.parse as _url
            b64 = _b64.b64encode(_json.dumps(tracks_payload, ensure_ascii=False).encode()).decode()
            button = {"text": "💾 Añadir a Wishlist", "url": f"https://reloadtrack.com/app.html?import={_url.quote(b64)}#wishlist"}
            keyboard = [[button]]

        try:
            r = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": msg,
                    "parse_mode": "Markdown",
                    "reply_markup": {"inline_keyboard": keyboard},
                },
                timeout=10,
            )
            if r.status_code != 200:
                print(f"Telegram API Error (Final): {r.status_code} {r.text}", flush=True)
        except Exception as e:
            print(f"Telegram send error: {e}", flush=True)

    except Exception as e:
        send_telegram(bot_token, chat_id, f"❌ Error en fingerprinting: {str(e)[:150]}")



# ── Streamrip download queue (sequential, persistent) ────────────────────────
import uuid as _uuid, queue as _queue, json as _json

DOWNLOAD_DIR = "/Volumes/X9 Pro/Musica/ReloadTrack/TagPending"
RIP_CMD = os.path.expanduser("~/.local/bin/rip")
STREAMRIP_CONFIG = os.path.expanduser("~/Library/Application Support/streamrip/config.toml")
DL_JOBS_FILE = os.path.expanduser("~/.local/share/reloadtrack/download_jobs.json")
XML_UPDATE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyze_and_update_xml.py")
XML_WATCH_DIR = "/Volumes/Musica/ReloadTrack"

os.makedirs(os.path.dirname(DL_JOBS_FILE), exist_ok=True)

# ── XML auto-update (debounced 30s) ──────────────────────────────────────────
_xml_timer      = None
_xml_timer_lock = threading.Lock()

def _run_xml_update():
    global _xml_timer
    _xml_timer = None
    if not os.path.exists(XML_UPDATE_SCRIPT):
        print(f"[xml] Script not found: {XML_UPDATE_SCRIPT}", flush=True)
        return
    print("[xml] Actualizando reloadtrack_cues.xml...", flush=True)
    try:
        r = subprocess.run(["python3", XML_UPDATE_SCRIPT],
                           capture_output=True, text=True, timeout=900)
        last = (r.stdout or r.stderr).strip().splitlines()
        print(f"[xml] ✓ {last[-1] if last else 'done'}", flush=True)
    except Exception as e:
        print(f"[xml] ✗ Error: {e}", flush=True)

def _schedule_xml_update():
    global _xml_timer
    with _xml_timer_lock:
        if _xml_timer:
            _xml_timer.cancel()
        _xml_timer = threading.Timer(30.0, _run_xml_update)
        _xml_timer.daemon = True
        _xml_timer.start()
        print("[xml] XML update programado en 30s", flush=True)

_download_jobs = {}   # job_id -> {status, artist, title, deezer_url, error}
_dl_queue = _queue.Queue()

def _save_jobs():
    try:
        with open(DL_JOBS_FILE, "w") as f:
            _json.dump(_download_jobs, f)
    except Exception as e:
        print(f"[dl] save_jobs error: {e}", flush=True)

def _load_jobs():
    try:
        with open(DL_JOBS_FILE) as f:
            saved = _json.load(f)
        for jid, info in saved.items():
            _download_jobs[jid] = info
            # Re-queue items that were queued/downloading (interrupted)
            if info.get("status") in ("queued", "downloading"):
                info["status"] = "queued"
                _dl_queue.put((jid, info.get("deezer_url",""), info.get("artist",""), info.get("title",""), info.get("folder", None)))
        print(f"[dl] Loaded {len(saved)} jobs from disk, re-queued pending", flush=True)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[dl] load_jobs error: {e}", flush=True)

def _dl_worker():
    """Single background thread — processes one download at a time, tracking progress."""
    import time as _time, re as _re
    while True:
        job_id, deezer_url, artist, title, dest_folder = _dl_queue.get()
        if job_id not in _download_jobs:
            _dl_queue.task_done()
            continue
        job = _download_jobs[job_id]
        job["status"]   = "downloading"
        job["progress"] = ""
        job["started"]  = _time.time()
        _save_jobs()
        print(f"[dl] {job_id} — {artist} - {title}", flush=True)
        try:
            skipped_by_db = False
            target_dir = dest_folder if dest_folder else DOWNLOAD_DIR
            os.makedirs(target_dir, exist_ok=True)
            print(f"[dl] {job_id}: folder → {target_dir}", flush=True)
            proc = subprocess.Popen(
                [RIP_CMD, "--config-path", STREAMRIP_CONFIG,
                 "--folder", target_dir, "--no-db", "url", deezer_url],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            # Read output line-by-line to capture progress
            pct_re = _re.compile(r"(\d{1,3})\s*%")
            for line in iter(proc.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                if "skipping" in line.lower() and "downloaded" in line.lower():
                    skipped_by_db = True
                # Extract percentage if present
                m = pct_re.search(line)
                if m:
                    job["progress"] = f"{m.group(1)}%"
                elif any(kw in line.lower() for kw in ("download", "convert", "tag", "saving")):
                    job["progress"] = line[:60]
                print(f"[dl] {job_id}: {line}", flush=True)
            proc.wait(timeout=360)
            if proc.returncode == 0 and not skipped_by_db:
                job["status"]   = "done"
                job["progress"] = "100%"
                print(f"[dl] ✓ {job_id} done — {artist} - {title}", flush=True)
                # Auto-update XML if downloaded to ReloadTrack library
                if XML_WATCH_DIR in target_dir:
                    _schedule_xml_update()
            else:
                reason = "skipped by DB" if skipped_by_db else f"exit code {proc.returncode}"
                job["status"] = "error"
                job["error"]  = reason
                print(f"[dl] ✗ {job_id} error: {reason}", flush=True)
        except Exception as e:
            job["status"] = "error"
            job["error"]  = str(e)
            print(f"[dl] ✗ {job_id} exception: {e}", flush=True)
        job.pop("started", None)
        _save_jobs()
        _dl_queue.task_done()

# Load persisted jobs and start worker on module load
_load_jobs()
threading.Thread(target=_dl_worker, daemon=True).start()

@app.route("/download-async", methods=["POST"])
def download_async():
    auth_error = check_secret()
    if auth_error:
        return auth_error

    body = request.json or {}
    deezer_url  = body.get("deezer_url", "").strip()
    artist      = body.get("artist", "").strip()
    title       = body.get("title", "").strip()
    dest_folder = body.get("folder", "").strip() or None  # e.g. /Volumes/Musica/ReloadTrack/Remember

    if not deezer_url:
        return jsonify({"error": "Missing deezer_url"}), 400

    # Deduplicate: skip if same URL already queued or done
    for jid, info in _download_jobs.items():
        if info.get("deezer_url") == deezer_url and info.get("status") in ("queued","downloading","done"):
            return jsonify({"ok": True, "job_id": jid, "status": info["status"], "duplicate": True})

    # Deduplicate: check if file already exists on disk across all music folders
    if artist or title:
        import unicodedata, re as _re
        def _norm(s):
            s = unicodedata.normalize("NFKD", s.lower())
            s = "".join(c for c in s if not unicodedata.combining(c))
            return _re.sub(r"[^a-z0-9]", "", s)
        artist_n = _norm(artist)
        title_n  = _norm(title)
        music_roots = [DOWNLOAD_DIR] if DOWNLOAD_DIR else []
        # Also scan sibling folders (House, HouseMash, Remember, etc.)
        parent = os.path.dirname(DOWNLOAD_DIR) if DOWNLOAD_DIR else None
        if parent and os.path.isdir(parent):
            for d in os.listdir(parent):
                fp = os.path.join(parent, d)
                if os.path.isdir(fp) and fp not in music_roots:
                    music_roots.append(fp)
        for root in music_roots:
            if not os.path.isdir(root):
                continue
            for fname in os.listdir(root):
                if fname.startswith("._"):
                    continue
                fname_n = _norm(fname)
                if artist_n and title_n:
                    if artist_n[:8] in fname_n and title_n[:8] in fname_n:
                        folder_name = os.path.basename(root)
                        return jsonify({"ok": True, "duplicate": True, "already_on_disk": True,
                                        "found_file": fname, "found_folder": folder_name})
                elif title_n and len(title_n) > 6 and title_n in fname_n:
                    folder_name = os.path.basename(root)
                    return jsonify({"ok": True, "duplicate": True, "already_on_disk": True,
                                    "found_file": fname, "found_folder": folder_name})

    job_id = str(_uuid.uuid4())[:8]
    _download_jobs[job_id] = {
        "status": "queued", "artist": artist, "title": title,
        "deezer_url": deezer_url, "folder": dest_folder, "error": None
    }
    _save_jobs()
    _dl_queue.put((job_id, deezer_url, artist, title, dest_folder))
    queue_pos = _dl_queue.qsize()
    return jsonify({"ok": True, "job_id": job_id, "status": "queued", "queue_position": queue_pos})


@app.route("/clear-done", methods=["POST", "GET"])
def clear_done():
    auth_error = check_secret()
    if auth_error: return auth_error
    global _download_jobs
    before = len(_download_jobs)
    _download_jobs = {k: v for k, v in _download_jobs.items()
                      if v.get("status") not in ("done", "error")}
    _save_jobs()
    return jsonify({"ok": True, "removed": before - len(_download_jobs), "remaining": len(_download_jobs)})


@app.route("/download-status")
def download_status():
    auth_error = check_secret()
    if auth_error:
        return auth_error
    job_id = request.args.get("id", "")
    if job_id not in _download_jobs:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify({"ok": True, "job_id": job_id, **_download_jobs[job_id]})


@app.route("/downloads-list")
def downloads_list():
    auth_error = check_secret()
    if auth_error:
        return auth_error
    files = []
    for ext in ["mp3", "flac", "m4a"]:
        files.extend(glob.glob(os.path.join(DOWNLOAD_DIR, "**", f"*.{ext}"), recursive=True))

    import time as _time2
    jobs = []
    totals = {"queued": 0, "downloading": 0, "done": 0, "error": 0}
    for jid, info in _download_jobs.items():
        st = info.get("status", "unknown")
        totals[st] = totals.get(st, 0) + 1
        elapsed = ""
        if st == "downloading" and info.get("started"):
            secs = int(_time2.time() - info["started"])
            elapsed = f"{secs//60}m{secs%60:02d}s" if secs >= 60 else f"{secs}s"
        jobs.append({
            "id":       jid,
            "status":   st,
            "artist":   info.get("artist", ""),
            "title":    info.get("title", ""),
            "progress": info.get("progress", ""),
            "elapsed":  elapsed,
        })
    # Active/queued first, then done, then error
    order = {"downloading": 0, "queued": 1, "done": 2, "error": 3}
    jobs.sort(key=lambda j: (order.get(j["status"], 9),))
    disk_ok = os.path.isdir(DOWNLOAD_DIR)
    disk_error = None if disk_ok else f"Disco no accesible: {DOWNLOAD_DIR}"
    return jsonify({"ok": True, "downloads": jobs, "totals": totals, "file_count": len(files), "folder": DOWNLOAD_DIR, "disk_ok": disk_ok, "disk_error": disk_error})

@app.route("/health")
def health():
    return jsonify({"ok": True, "mode": "local-residential"})

# ── Local collection check ────────────────────────────────────────────────────
import unicodedata as _udata

def _norm(s):
    """Lowercase, strip accents, keep alphanum+spaces+parens for comparison."""
    s = s.lower().strip()
    s = _udata.normalize('NFD', s)
    s = ''.join(c for c in s if _udata.category(c) != 'Mn')
    s = re.sub(r'[^\w\s()\[\]-]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def _extract_version(title):
    """Split title into (base, version). 'Get Lucky (Radio Edit)' → ('get lucky', 'radio edit')."""
    m = re.search(r'[\(\[](.*?)[\)\]]', title)
    if m:
        return title[:m.start()].strip(), m.group(1).strip()
    return title.strip(), ''

def _match_local(artist, title, norm_files):
    norm_a = _norm(artist)
    norm_t = _norm(title)
    base_t, ver_t = _extract_version(norm_t)
    for norm_fname, fpath in norm_files:
        if norm_a and norm_a not in norm_fname:
            continue
        if norm_t in norm_fname:                              # exact (incl. version)
            return fpath, 1.0
        if base_t and base_t in norm_fname and not ver_t:    # no version in query → any match ok
            return fpath, 0.85
    return None, 0

@app.route("/check-local-bulk", methods=["POST"])
def check_local_bulk():
    auth_error = check_secret()
    if auth_error: return auth_error
    if not os.path.isdir(DOWNLOAD_DIR):
        return jsonify({"ok": False, "disk_ok": False,
                        "error": f"Disco no accesible: {DOWNLOAD_DIR}"}), 503
    body = request.get_json(force=True) or {}
    tracks = body.get("tracks", [])

    # Build normalised file list once
    all_files = []
    for ext in ["mp3", "flac", "m4a", "wav", "aiff", "ogg"]:
        all_files.extend(glob.glob(os.path.join(DOWNLOAD_DIR, "**", f"*.{ext}"), recursive=True))
    norm_files = [(_norm(os.path.splitext(os.path.basename(f))[0]), f) for f in all_files]

    results = []
    for t in tracks:
        artist = t.get("artist", "")
        title  = t.get("title",  "")
        fpath, conf = _match_local(artist, title, norm_files)
        results.append({"artist": artist, "title": title,
                         "found": fpath is not None,
                         "path": fpath or "", "confidence": conf})

    found_n = sum(1 for r in results if r["found"])
    return jsonify({"ok": True, "results": results,
                    "checked": len(tracks), "found_count": found_n,
                    "total_files": len(all_files)})




# ── Spotify playlist extraction via proxy (residential IP) ───────────────────
import urllib.request as _urllib_req
import urllib.error as _urllib_err

def _spotify_api_fetch(path, token):
    req = _urllib_req.Request(
        f"https://api.spotify.com/v1/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with _urllib_req.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except _urllib_err.HTTPError as e:
        body = e.read()
        try:
            return json.loads(body)
        except Exception:
            return {"error": {"status": e.code, "message": str(e)}}


@app.route("/spotify-playlist", methods=["POST"])

def spotify_playlist():
    auth_error = check_secret()
    if auth_error: return auth_error

    body = request.get_json(force=True) or {}
    playlist_url = body.get("url", "").strip()
    client_id     = body.get("client_id", "").strip()
    client_secret = body.get("client_secret", "").strip()
    user_token    = body.get("user_token", "").strip()   # optional: user OAuth token
    force_playwright = body.get("force_playwright", False)

    playlist_id = None
    m = re.search(r"spotify\.com/playlist/([A-Za-z0-9]+)", playlist_url)
    if m:
        playlist_id = m.group(1)
    if not playlist_id:
        return jsonify({"error": "Missing or invalid Spotify playlist URL"}), 400

    # Skip API entirely — go straight to Playwright with authenticated Chrome session
    if force_playwright:
        return _ytdlp_chrome_cookies_extract(
            playlist_id,
            chat_id=body.get("chat_id"),
            bot_token=body.get("bot_token"),
            callback_url=body.get("callback_url"),
            callback_secret=body.get("callback_secret"),
            queue_id=body.get("queue_id"),
            pl_name=body.get("pl_name", ""),
        )

    try:
        import base64

        # ── Step 1: get access token ─────────────────────────────────────────
        if user_token:
            token = user_token
        elif client_id and client_secret:
            try:
                creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                req = _urllib_req.Request(
                    "https://accounts.spotify.com/api/token",
                    data=b"grant_type=client_credentials",
                    headers={
                        "Authorization": f"Basic {creds_b64}",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": "Mozilla/5.0",
                    },
                    method="POST"
                )
                with _urllib_req.urlopen(req, timeout=15) as resp:
                    token_data = json.loads(resp.read())
                    token = token_data.get("access_token", "")
                if not token:
                    return jsonify({"ok": False, "error": "No se pudo obtener token de Spotify (credenciales inválidas)", "tracks": []})
            except _urllib_err.HTTPError as e:
                body = e.read()
                try:
                    err_json = json.loads(body)
                    err_msg = err_json.get("error_description") or err_json.get("error") or str(e)
                except Exception:
                    err_msg = str(e)
                return jsonify({"ok": False, "error": f"Spotify auth error {e.code}: {err_msg}", "tracks": []})
        else:
            return jsonify({"ok": False, "error": "Se necesitan client_id y client_secret o user_token", "tracks": []}), 400


        # ── Step 2: fetch playlist tracks ────────────────────────────────────
        tracks = []
        offset = 0
        total = None
        pl_name = ""

        # Get playlist name first
        try:
            pl_data = _spotify_api_fetch(f"playlists/{playlist_id}?fields=name,tracks.total", token)
            pl_name = pl_data.get("name", "")
            total = pl_data.get("tracks", {}).get("total", 0)
        except Exception:
            pass

        while True:
            data = _spotify_api_fetch(
                f"playlists/{playlist_id}/tracks?limit=50&offset={offset}&market=ES&fields=next,items(track(name,artists,is_local,type))",
                token
            )

            if "error" in data:
                api_status = data['error'].get('status')
                api_msg    = data['error'].get('message', '')

                # ── 403: try yt-dlp with Chrome cookies (user's browser session) ──
                if api_status == 403:
                    return _ytdlp_chrome_cookies_extract(
                        playlist_id,
                        chat_id=body.get("chat_id"),
                        bot_token=body.get("bot_token"),
                        callback_url=body.get("callback_url"),
                        callback_secret=body.get("callback_secret"),
                        queue_id=body.get("queue_id"),
                        pl_name=body.get("pl_name", ""),
                    )

                return jsonify({
                    "ok": False,
                    "error": f"Spotify API: {api_status} {api_msg}",
                    "tracks": []
                })

            for item in data.get("items", []):
                t = item.get("track")
                if not t or not t.get("name"):
                    continue
                if t.get("type") == "episode":
                    continue
                artist = ", ".join(a["name"] for a in t.get("artists", []) if a.get("name"))
                tracks.append({"artist": artist, "title": t["name"], "local": t.get("is_local", False)})

            if not data.get("next") or len(tracks) >= 500:
                break
            offset += 50

        return jsonify({"ok": True, "tracks": tracks, "count": len(tracks), "total": total, "name": pl_name})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "tracks": []}), 500


def _ytdlp_chrome_cookies_extract(playlist_id, chat_id=None, bot_token=None,
                                   callback_url=None, callback_secret=None,
                                   queue_id=None, pl_name=""):
    """Playwright + system Chrome + Chrome session cookies to scrape Spotify.
    If callback_url is provided, runs async in background thread and returns immediately."""
    import threading

    def _run(playlist_id, chat_id, bot_token, callback_url, callback_secret, queue_id, pl_name):
        result = _playwright_scrape(playlist_id)
        tracks = result.get("tracks", [])

        import urllib.request as _ur2, json as _json2, urllib.parse as _up

        # 1. Save tracks to D1 via save-queue endpoint
        _UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
        save_url = "https://reloadtrack-app.pages.dev/api/save-queue"
        try:
            payload = _json2.dumps({"queue_id": queue_id, "tracks": tracks}).encode()
            req = _ur2.Request(
                f"{save_url}?secret={_up.quote(callback_secret)}",
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": _UA},
                method="POST"
            )
            _ur2.urlopen(req, timeout=30)
            print(f"[playwright] save-queue OK: {len(tracks)} tracks → {queue_id}")
        except Exception as e:
            print(f"[playwright] save-queue failed: {e}")

        # 2. Send results directly to Telegram
        def tg(method, **kwargs):
            body = _json2.dumps({"chat_id": chat_id, **kwargs}).encode()
            r = _ur2.Request(
                f"https://api.telegram.org/bot{bot_token}/{method}",
                data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            return _json2.loads(_ur2.urlopen(r, timeout=15).read())

        if not tracks:
            err = result.get("error", "Sin detalles")
            tg("sendMessage", text=f"❌ No se encontraron tracks.\n_{err}_", parse_mode="Markdown")
            return

        # Send track list in chunks
        header = f"🎧 *{pl_name or 'Playlist'}* — {len(tracks)} tracks:\n\n"
        chunk = header
        for i, t in enumerate(tracks):
            line = f"{i+1}. {t.get('artist','')+' - ' if t.get('artist') else ''}{t.get('title','')}\n"
            if len(chunk) + len(line) > 3900:
                tg("sendMessage", text=chunk, parse_mode="Markdown")
                chunk = ""
            chunk += line
        if chunk:
            tg("sendMessage", text=chunk, parse_mode="Markdown")

        # Send summary + action buttons
        tg("sendMessage",
           text=f"✅ *{len(tracks)} tracks* extraídos.",
           parse_mode="Markdown",
           reply_markup=_json2.dumps({"inline_keyboard": [[
               {"text": "📥 Descargar todo", "callback_data": f"wldl:{queue_id}"},
               {"text": "🏷️ Añadir TAG",    "callback_data": f"wltag:{queue_id}"},
           ]]})
        )

    if callback_url and chat_id:
        # Async mode: start background thread, return immediately
        t = threading.Thread(target=_run, args=(
            playlist_id, chat_id, bot_token, callback_url, callback_secret, queue_id, pl_name
        ), daemon=True)
        t.start()
        return jsonify({"ok": True, "async": True, "status": "processing"})
    else:
        # Sync mode (no callback URL)
        result = _playwright_scrape(playlist_id)
        return jsonify(result)


def _playwright_scrape(playlist_id):
    """Runs Playwright synchronously and returns a dict with ok/tracks/etc."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return jsonify({"ok": False,
                        "error": "Playwright no instalado. Ejecuta: pip install playwright",
                        "tracks": []})

    # JS: extract visible tracks from the virtual list
    EXTRACT_JS = """
    () => {
        const rows = document.querySelectorAll('[data-testid="tracklist-row"]');
        return Array.from(rows).map(row => {
            const titleEl = row.querySelector('[data-testid="internal-track-link"] span')
                         || row.querySelector('[data-testid="internal-track-link"]');
            const title   = titleEl?.textContent?.trim() || '';
            const artists = Array.from(
                row.querySelectorAll('span a[href*="/artist/"]')
            ).map(a => a.textContent?.trim()).filter(Boolean);
            const idx = row.getAttribute('aria-rowindex') || title;
            return { idx, title, artist: artists.join(', ') };
        }).filter(t => t.title);
    }
    """

    # JS: find Spotify's internal scroll container and scroll it
    SCROLL_JS = """
    () => {
        // Spotify uses overlayscrollbars or a custom scrollable div — find it
        const candidates = [
            document.querySelector('[data-overlayscrollbars-viewport]'),
            document.querySelector('.main-view-container__scroll-node'),
            document.querySelector('[data-testid="main-view-container"] > div'),
            document.querySelector('main'),
        ];
        for (const el of candidates) {
            if (el && el.scrollHeight > el.clientHeight + 10) {
                el.scrollBy(0, 800);
                return `scrolled:${el.tagName}.${el.className.slice(0,40)}`;
            }
        }
        // Fallback: scroll the window
        window.scrollBy(0, 800);
        return 'scrolled:window';
    }
    """

    # ── Extract Chrome Spotify cookies via yt-dlp (handles encryption correctly) ─
    pw_cookies = []
    try:
        import tempfile, glob as _glob, subprocess as _sp2
        chrome_base = os.path.expanduser("~/Library/Application Support/Google/Chrome")

        # Try each Chrome profile until we find one with sp_dc
        profiles = ["Profile 1", "Default"] + [
            os.path.basename(p) for p in _glob.glob(f"{chrome_base}/Profile *")
        ]
        for profile in profiles:
            cookie_file = tempfile.mktemp(suffix=".cookies.txt")
            try:
                # yt-dlp --cookies-from-browser correctly decrypts Chrome cookies
                result = _sp2.run(
                    YT_DLP + [
                        "--cookies-from-browser", f"chrome:{profile}",
                        "--cookies", cookie_file,
                        "--skip-download", "--no-warnings", "--quiet",
                        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
                    ],
                    capture_output=True, text=True, timeout=30
                )
                # Parse Netscape cookie file
                if os.path.exists(cookie_file):
                    MAX_TS = 2147483647  # Playwright max valid timestamp
                    with open(cookie_file) as f:
                        for line in f:
                            if line.startswith("#") or not line.strip(): continue
                            parts = line.strip().split("\t")
                            if len(parts) < 7: continue
                            domain, _, path, secure, expires, name, value = parts[:7]
                            if domain not in (".spotify.com", "open.spotify.com"): continue
                            try:
                                exp = int(expires)
                                exp = max(-1, min(exp, MAX_TS))  # clamp to valid range
                            except ValueError:
                                exp = -1
                            entry = {
                                "name": name, "value": value,
                                "domain": domain, "path": path,
                                "secure": secure == "TRUE",
                                "expires": exp,
                            }
                            pw_cookies.append(entry)
                    if any(c["name"] == "sp_dc" for c in pw_cookies):
                        break  # found authenticated profile
                    pw_cookies = []  # reset and try next profile
            except Exception as e:
                pw_cookies = []
            finally:
                if os.path.exists(cookie_file):
                    try: os.unlink(cookie_file)
                    except: pass
    except Exception:
        pw_cookies = []


    has_auth     = any(c["name"] == "sp_dc" for c in pw_cookies)
    tracks_map   = {}
    playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="es-ES",
                viewport={"width": 1280, "height": 900},
                extra_http_headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}
            )

            if pw_cookies:
                try:
                    ctx.add_cookies(pw_cookies)
                except Exception:
                    pass

            page = ctx.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            page.goto(playlist_url, wait_until="domcontentloaded", timeout=30000)

            try:
                page.wait_for_selector(
                    '[data-testid="tracklist-row"], [data-testid="login-button"]',
                    timeout=20000
                )
            except PWTimeout:
                browser.close()
                return jsonify({"ok": False,
                                "error": "Timeout: Spotify no cargó el tracklist",
                                "tracks": []})

            # Move mouse to center so wheel events hit the playlist container
            page.mouse.move(640, 450)

            # ── Scroll with mouse.wheel() — the only method Spotify responds to ──
            no_change  = 0
            prev_count = 0
            for _ in range(120):           # up to ~1000 tracks
                rows = page.evaluate(EXTRACT_JS)
                for r in rows:
                    tracks_map[r["idx"]] = {"artist": r["artist"], "title": r["title"]}

                page.mouse.wheel(0, 800)   # simulate real mouse wheel
                page.wait_for_timeout(400)

                if len(tracks_map) == prev_count:
                    no_change += 1
                else:
                    no_change  = 0
                    prev_count = len(tracks_map)
                if no_change >= 8:
                    break

            browser.close()

        tracks = list(tracks_map.values())
        if not tracks:
            note = " — inicia sesión en Spotify en Chrome para ver playlists privadas" if not has_auth else ""
            return {"ok": False, "error": f"No se encontraron tracks en el DOM{note}", "tracks": []}

        via = "playwright-auth" if has_auth else "playwright-anon"
        return {"ok": True, "tracks": tracks, "count": len(tracks),
                "total": len(tracks), "name": "", "via": via}

    except Exception as e:
        return {"ok": False, "error": str(e), "tracks": []}


@app.route("/audio-url")

def audio_url_endpoint():
    auth_error = check_secret()
    if auth_error:
        return auth_error
    video_id = request.args.get("v", "").strip()
    if not validate_video_id(video_id):
        return jsonify({"error": "Invalid video_id"}), 400
    try:
        r = subprocess.run(
            YT_DLP + ["--no-playlist", "--no-warnings", "--quiet",
                      "--format", "18",
                      "--print", "%(title)s|||%(duration)s|||%(url)s",
                      f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=45,
        )
        out = r.stdout.strip()
        if out and "|||" in out:
            parts = out.split("|||", 2)
            if len(parts) == 3:
                title, dur, url = parts
                return jsonify({"title": title.strip(), "duration_secs": int(dur.strip()) if dur.strip().isdigit() else 0, "url": url.strip()})
        return jsonify({"error": "yt-dlp failed", "detail": r.stderr.strip()[:300]}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/fingerprint-async", methods=["POST"])
def fingerprint_async():
    """Start fingerprinting in a background thread. Returns immediately.
    Accepts either {video_id} for YouTube or {video_url} for any other platform."""
    auth_error = check_secret()
    if auth_error:
        return auth_error

    body = request.json or {}
    video_id  = body.get("video_id", "").strip()
    video_url = body.get("video_url", "").strip()
    chat_id   = body.get("chat_id")
    bot_token = body.get("bot_token", "")

    if not chat_id or not bot_token:
        return jsonify({"error": "Missing chat_id or bot_token"}), 400

    if video_id:
        if not validate_video_id(video_id):
            return jsonify({"error": "Invalid video_id"}), 400
        t = threading.Thread(target=fingerprint_worker, args=(video_id, chat_id, bot_token), daemon=True)
    elif video_url:
        cache_id = hashlib.md5(video_url.encode()).hexdigest()[:8]
        t = threading.Thread(target=fingerprint_worker_url, args=(video_url, cache_id, chat_id, bot_token), daemon=True)
    else:
        return jsonify({"error": "Missing video_id or video_url"}), 400

    t.start()
    return jsonify({"ok": True, "message": "Fingerprinting started in background"})


# ── Channel bulk scraping ─────────────────────────────────────────────────────

def _ivoox_get_episode_urls(channel_url):
    """Extract all episode URLs from an iVoox channel/show using the RSS feed.
    Paginates through all RSS pages. Returns list of episode page URLs."""
    import xml.etree.ElementTree as ET

    # 1. Resolve short URL / get canonical → extract show ID
    try:
        r = requests.head(channel_url, allow_redirects=True, timeout=10)
        canonical = r.url
    except Exception:
        canonical = channel_url

    # Extract show numeric ID from URL patterns like _sq_f1838533_ or _fg_f1838533_
    m = re.search(r'_(?:sq|fg)_f(\d+)_', canonical)
    if not m:
        # Try from the original URL
        m = re.search(r'_(?:sq|fg)_f(\d+)_', channel_url)
    if not m:
        # Try /sq/XXXXXX short form — use the number
        m = re.search(r'/sq/(\d+)', channel_url)
    if not m:
        return [], canonical

    show_id = m.group(1)
    print(f"[channel] iVoox show ID: {show_id}", flush=True)

    # 2. Paginate RSS feed — feeds.ivoox.com/feed_fg_f{ID}_filtro_{PAGE}.xml
    episode_urls = []
    page = 1
    while True:
        rss_url = f"https://feeds.ivoox.com/feed_fg_f{show_id}_filtro_{page}.xml"
        try:
            resp = requests.get(rss_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if not resp.ok:
                break
            root = ET.fromstring(resp.text)
            items = root.findall('.//item')
            if not items:
                break
            for item in items:
                link = item.findtext('link')
                if link and ('ivoox.com' in link):
                    episode_urls.append(link.strip())
            print(f"[channel] RSS page {page}: {len(items)} episodes", flush=True)
            if len(items) < 20:  # last page (iVoox RSS paginates at 20)
                break
            page += 1
        except Exception as e:
            print(f"[channel] RSS page {page} error: {e}", flush=True)
            break

    return episode_urls, canonical


def _ivoox_get_episode_urls(channel_url):
    """Extract all episode URLs from an iVoox channel/show via RSS feed (paginated)."""
    import xml.etree.ElementTree as ET

    # Resolve short URL to get canonical with show ID
    try:
        r = requests.head(channel_url, allow_redirects=True, timeout=10,
                          headers={"User-Agent": "Mozilla/5.0"})
        canonical = r.url
    except Exception:
        canonical = channel_url

    # Extract show ID from _sq_f1838533_ / _fg_f1838533_ / /sq/1838533
    m = (re.search(r'_(?:sq|fg)_f(\d+)_', canonical) or
         re.search(r'_(?:sq|fg)_f(\d+)_', channel_url) or
         re.search(r'/sq/(\d+)', channel_url))
    if not m:
        return [], canonical

    show_id = m.group(1)
    print(f"[channel] iVoox show ID: {show_id}", flush=True)

    # Paginate RSS: feeds.ivoox.com/feed_fg_f{ID}_filtro_{PAGE}.xml
    episode_urls = []
    page = 1
    while True:
        rss_url = f"https://feeds.ivoox.com/feed_fg_f{show_id}_filtro_{page}.xml"
        try:
            resp = requests.get(rss_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if not resp.ok:
                break
            root = ET.fromstring(resp.text)
            items = root.findall('.//item')
            if not items:
                break
            for item in items:
                link = item.findtext('link')
                if link and 'ivoox.com' in link:
                    episode_urls.append(link.strip())
            print(f"[channel] RSS page {page}: {len(items)} episodes", flush=True)
            if len(items) < 20:  # last page
                break
            page += 1
        except Exception as e:
            print(f"[channel] RSS page {page} error: {e}", flush=True)
            break

    return episode_urls, canonical



def _mixcloud_get_episode_urls(profile_url):
    """Fetch all cloudcast URLs from a Mixcloud profile via API."""
    import re
    m = re.search(r'mixcloud\.com/([^/?#]+)', profile_url)
    if not m:
        return [], profile_url
    username = m.group(1)
    shows = []
    next_url = f"https://api.mixcloud.com/{username}/cloudcasts/?fields=key,name&limit=100"
    pages = 0
    while next_url and pages < 10:
        try:
            r = requests.get(next_url, headers={"User-Agent": "ReloadTrack/1.0"}, timeout=15)
            if not r.ok:
                break
            d = r.json()
            shows.extend(d.get("data", []))
            next_url = d.get("paging", {}).get("next")
            pages += 1
        except Exception as e:
            print(f"[mixcloud_api] page {pages} error: {e}", flush=True)
            break
    urls = [f"https://www.mixcloud.com{s['key']}" for s in shows if s.get("key")]
    print(f"[mixcloud_api] {len(urls)} shows from @{username}", flush=True)
    return urls, f"https://www.mixcloud.com/{username}/"

def channel_worker(channel_url, chat_id, bot_token):
    """Background: extract all iVoox episodes via RSS, queue in D1, fingerprint sequentially."""
    try:
        send_telegram(bot_token, chat_id,
            "\U0001f4e1 Extrayendo episodios del canal...\n" + channel_url)

        if "mixcloud.com" in channel_url:
            raw_urls, canonical = _mixcloud_get_episode_urls(channel_url)
        else:
            raw_urls, canonical = _ivoox_get_episode_urls(channel_url)
        if not raw_urls:
            send_telegram(bot_token, chat_id,
                "\u274c No encontre episodios.\n"
                f"URL resuelta: {canonical}\n"
                "Asegurate de que es la URL del canal/perfil (no un episodio).")
            return

        print(f"[channel] {len(raw_urls)} episodes found for show {canonical}", flush=True)

        # Register in D1 (INSERT OR IGNORE skips already-done)
        episodes_payload = [{"url": u, "channel_url": channel_url} for u in raw_urls]
        reg = requests.post(
            f"{APP_BASE_URL}/api/episode-queue?secret={PROXY_SECRET}",
            json={"episodes": episodes_payload}, timeout=30,
        )

        reg_data = reg.json() if reg.ok else {}
        added   = reg_data.get("added", len(raw_urls))
        skipped = reg_data.get("skipped", 0)

        send_telegram(bot_token, chat_id,
            f"\U0001f4cb Canal: {len(raw_urls)} episodios\n"
            f"\U0001f195 {added} nuevos \u00b7 \u23ed\ufe0f {skipped} ya procesados (saltados)\n\n"
            f"\u23f3 Procesando 1 a 1... te ire avisando")

        if added == 0 and skipped > 0:
            # All already in DB — but check if any are still pending (previous cancelled run)
            pend_check = requests.get(
                f"{APP_BASE_URL}/api/episode-queue?action=pending", timeout=10
            )
            still_pending = pend_check.json().get("episodes", []) if pend_check.ok else []
            # Filter to only this channel's episodes
            still_pending = [e for e in still_pending if e.get("channel_url") == channel_url]
            if not still_pending:
                send_telegram(bot_token, chat_id, "\u2705 Todos los episodios ya estaban procesados.")
                return
            send_telegram(bot_token, chat_id,
                f"\u23ed\ufe0f {skipped} episodios ya en cola \u00b7 {len(still_pending)} pendientes de procesar.\n"
                "Continuando...")

        # Fetch pending and process sequentially
        pend = requests.get(f"{APP_BASE_URL}/api/episode-queue?action=pending", timeout=10)
        pending_eps = pend.json().get("episodes", []) if pend.ok else []
        total = len(pending_eps)
        total_new = 0
        total_dup = 0

        for idx, ep in enumerate(pending_eps, 1):
            ep_url = ep["url"]
            print(f"[channel] Ep {idx}/{total}: {ep_url}", flush=True)

            try:
                requests.post(
                    f"{APP_BASE_URL}/api/episode-queue?secret={PROXY_SECRET}&action=update"
                    f"&url={requests.utils.quote(ep_url, safe='')}&status=processing",
                    timeout=5)
            except Exception:
                pass

            try:
                title_ep, duration = get_url_info(ep_url)
                local_file = download_url(ep_url, hashlib.md5(ep_url.encode()).hexdigest()[:8])
                ep_new, ep_dup, ep_queue_id = _run_fingerprint_silent(
                    hashlib.md5(ep_url.encode()).hexdigest()[:8],
                    local_file, duration, chat_id, bot_token, ep_url, title_ep)
                total_new += ep_new
                total_dup += ep_dup
                requests.post(
                    f"{APP_BASE_URL}/api/episode-queue?secret={PROXY_SECRET}&action=update"
                    f"&url={requests.utils.quote(ep_url, safe='')}&status=done"
                    f"&tracks_new={ep_new}&tracks_dup={ep_dup}", timeout=5)

                # Build action buttons: wishlist + tag + deezer download
                buttons = []
                if ep_queue_id:
                    buttons.append({"text": "\U0001f4be A\xf1adir a Wishlist", "callback_data": f"wl:{ep_queue_id}"})
                    buttons.append({"text": "\U0001f3f7\ufe0f Con Tag...", "callback_data": f"wltag:{ep_queue_id}"})
                    buttons.append({"text": "\u2b07\ufe0f Descargar Deezer", "callback_data": f"wldl:{ep_queue_id}"})

                msg = (
                    f"\u2705 {idx}/{total}: *{title_ep}*\n"
                    f"   {ep_new} tracks nuevos \u00b7 {ep_dup} ya en wishlist"
                )
                try:
                    keyboard = [buttons] if buttons else []
                    requests.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": msg,
                            "parse_mode": "Markdown",
                            "reply_markup": {"inline_keyboard": keyboard},
                        },
                        timeout=10,
                    )
                except Exception as _te:
                    send_telegram(bot_token, chat_id, msg)
            except Exception as e:
                err = str(e)[:200]
                print(f"[channel] Error ep {ep_url}: {err}", flush=True)
                try:
                    requests.post(
                        f"{APP_BASE_URL}/api/episode-queue?secret={PROXY_SECRET}&action=update"
                        f"&url={requests.utils.quote(ep_url, safe='')}&status=error"
                        f"&error={requests.utils.quote(err, safe='')}", timeout=5)
                except Exception:
                    pass
                send_telegram(bot_token, chat_id, f"\u26a0\ufe0f Ep {idx}/{total} error: {err}")

        send_telegram(bot_token, chat_id,
            f"\U0001f389 Canal completado!\n"
            f"\U0001f4ca {total} episodios \u00b7 {total_new} tracks nuevos en wishlist \u00b7 {total_dup} duplicados saltados")

    except Exception as e:
        send_telegram(bot_token, chat_id, f"\u274c Error procesando canal: {str(e)[:200]}")
        print(f"[channel] FATAL: {e}", flush=True)

def _run_fingerprint_silent(label, local_file, duration, chat_id, bot_token, source_url, title):
    """Like _run_fingerprint but returns (tracks_new, tracks_dup) instead of sending Telegram msg.
    Used by channel_worker for batch processing with its own progress reporting."""
    import concurrent.futures as _cf
    samples = list(range(60, max(duration - 60, 61), SAMPLE_INTERVAL))
    if not samples:
        return 0, 0

    def process_one(t):
        result = shazam_recognize(local_file, t)
        if result:
            return result
        mp3 = extract_segment_local(local_file, t)
        if not mp3:
            return None
        return audd_recognize_bytes(mp3, t)

    tracks_raw = []
    with _cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_one, t): t for t in samples}
        for fut in _cf.as_completed(futures):
            res = fut.result()
            if res:
                tracks_raw.append(res)

    # Deduplicate
    seen = set()
    tracks = []
    for r in sorted(tracks_raw, key=lambda x: x["t"]):
        key = f"{r['artist']}|{r['title']}".lower()
        if key not in seen:
            seen.add(key)
            tracks.append(r)

    if not tracks:
        return 0, 0

    # Post to bot-queue, get stored/skipped count + queue_id
    tracks_payload = [{"artist": t["artist"], "title": t["title"]} for t in tracks]
    tracks_new = 0
    tracks_dup = 0
    queue_id   = None
    try:
        r = requests.post(
            f"{APP_BASE_URL}/api/bot-queue?secret={PROXY_SECRET}",
            json={"tracks": tracks_payload}, timeout=10,
        )
        if r.ok:
            rj = r.json()
            tracks_new = rj.get("stored", len(tracks))
            tracks_dup = rj.get("skipped", 0)
            queue_id   = rj.get("id")
    except Exception:
        pass

    # Extract transitions
    try:
        import re as _re
        dj_match = _re.search(r'(?:DJ|Dj|dj)[.\s]+([A-Za-záéíóúÁÉÍÓÚüÜñÑ\s]+?)(?:\s*[-–|]|$)', title)
        dj_name = dj_match.group(1).strip() if dj_match else ''
        ordered_raw = sorted(tracks_raw, key=lambda x: x["t"])
        transitions = []
        for i in range(len(ordered_raw) - 1):
            a, b = ordered_raw[i], ordered_raw[i + 1]
            if a["artist"] == b["artist"] and a["title"] == b["title"]:
                continue
            if b["t"] - a["t"] == SAMPLE_INTERVAL:
                transitions.append({
                    "a": {"artist": a["artist"], "title": a["title"]},
                    "b": {"artist": b["artist"], "title": b["title"]},
                    "dj_name": dj_name, "source_url": source_url,
                })
        if transitions:
            requests.post(
                f"{APP_BASE_URL}/api/mix-transitions?secret={PROXY_SECRET}",
                json={"transitions": transitions, "dj_name": dj_name, "source_url": source_url},
                timeout=10,
            )
    except Exception:
        pass

    return tracks_new, tracks_dup, queue_id


@app.route("/channel-enqueue", methods=["POST"])
def channel_enqueue():
    """Enqueue all episodes from a channel URL for sequential fingerprinting."""
    auth_error = check_secret()
    if auth_error:
        return auth_error

    body = request.json or {}
    channel_url = body.get("url", "").strip()
    chat_id     = body.get("chat_id")
    bot_token   = body.get("bot_token", "")

    if not channel_url or not chat_id or not bot_token:
        return jsonify({"error": "Missing url, chat_id or bot_token"}), 400

    t = threading.Thread(target=channel_worker, args=(channel_url, chat_id, bot_token), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Channel processing started"})




def _search_deezer(artist, title):
    """Search Deezer API for a track. Returns deezer URL or None."""
    try:
        q = f"{artist} {title}".strip()
        r = requests.get(
            "https://api.deezer.com/search",
            params={"q": q, "limit": 1},
            timeout=8
        )
        if r.ok:
            data = r.json().get("data", [])
            if data:
                return f"https://www.deezer.com/track/{data[0]['id']}"
    except Exception as e:
        print(f"[deezer_search] {artist} - {title}: {e}", flush=True)
    return None


def _batch_download_worker(tracks, chat_id, bot_token):
    """Background: search Deezer for each track and download matches via streamrip."""
    found = []
    not_found = []

    send_telegram(bot_token, chat_id,
        f"\U0001f50d Buscando en Deezer {len(tracks)} tracks...\n"
        "_Descargando los que encuentre..._"
    )

    for t in tracks:
        artist = t.get("artist", "")
        title  = t.get("title", "")
        deezer_url = _search_deezer(artist, title)
        if deezer_url:
            found.append({"artist": artist, "title": title, "url": deezer_url})
        else:
            not_found.append(f"{artist} — {title}" if artist else title)

    if not found:
        send_telegram(bot_token, chat_id,
            f"\u274c No encontré ningún track en Deezer de los {len(tracks)} analizados.\n"
            "Puede que los nombres sean demasiado específicos o no estén disponibles."
        )
        return

    send_telegram(bot_token, chat_id,
        f"\u2705 {len(found)}/{len(tracks)} tracks encontrados en Deezer\n"
        f"\U0001f4e5 Descargando {len(found)} tracks...\n"
        f"_(destino: /Volumes/Musica/ReloadTrack)_"
    )

    downloaded = 0
    errors = 0
    for t in found:
        try:
            cmd = ["rip", "url", t["url"]]
            print(f"[batch_dl] rip url {t['url']}", flush=True)
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                downloaded += 1
                print(f"[batch_dl] ✓ {t['artist']} — {t['title']}", flush=True)
            else:
                errors += 1
                err = (result.stderr or result.stdout or "")[-100:]
                print(f"[batch_dl] ✗ {t['artist']} — {t['title']}: {err}", flush=True)
        except Exception as e:
            errors += 1
            print(f"[batch_dl] exception {t['title']}: {e}", flush=True)

    lines = [f"  • {t['artist']} — {t['title']}" if t['artist'] else f"  • {t['title']}" for t in found[:20]]
    summary = "\n".join(lines) + ("\n  ..." if len(found) > 20 else "")

    send_telegram(bot_token, chat_id,
        f"\U0001f389 *Descarga completada*\n"
        f"\u2705 {downloaded} descargados \u00b7 \u274c {errors} errores\n\n"
        f"*Tracks descargados:*\n{summary}"
    )


@app.route("/batch-download", methods=["POST"])
def batch_download():
    """Enqueue batch Deezer search + download for a list of tracks."""
    auth_error = check_secret()
    if auth_error:
        return auth_error

    body      = request.json or {}
    tracks    = body.get("tracks", [])
    chat_id   = body.get("chat_id")
    bot_token = body.get("bot_token", "")

    if not tracks or not chat_id or not bot_token:
        return jsonify({"error": "Missing tracks, chat_id or bot_token"}), 400

    t = threading.Thread(
        target=_batch_download_worker,
        args=(tracks, chat_id, bot_token),
        daemon=True
    )
    t.start()
    return jsonify({"ok": True, "queued": len(tracks)})

def _polling_daemon():
    import time
    import requests
    import shutil
    import json
    
    API_QUEUE_URL = "https://reloadtrack-app.pages.dev/api/queue"
    DOWNLOAD_FOLDER = "/Volumes/X9 Pro/Musica/ReloadTrack/TagPending"
    
    def get_executable(name):
        path = shutil.which(name)
        if path: return path
        home = os.path.expanduser('~')
        if os.path.exists(f"{home}/.local/bin/{name}"):
            return f"{home}/.local/bin/{name}"
        return name

    print("[polling] Daemon de colas iniciado en proxy_local...", flush=True)
    while True:
        try:
            r = requests.get(f"{API_QUEUE_URL}?pop=1&secret={PROXY_SECRET}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get('pending') and data.get('task'):
                    task = data['task']
                    url = task.get('url')
                    provider = task.get('provider')
                    source_str = task.get('source', '{}')
                    
                    try:
                        source = json.loads(source_str)
                    except:
                        source = {}
                        
                    chat_id = source.get('chat_id', '')
                    bot_token = source.get('bot_token', '')
                    
                    success = True
                    if provider == 'fingerprint':
                        cache_id = task['id']
                        print(f"[polling] Iniciando fingerprint_worker_url para {url}")
                        threading.Thread(target=fingerprint_worker_url, args=(url, cache_id, chat_id, bot_token), daemon=True).start()
                    elif provider == 'channel-enqueue':
                        print(f"[polling] Iniciando channel_enqueue_worker para {url}")
                        threading.Thread(target=channel_enqueue_worker, args=(url, chat_id, bot_token), daemon=True).start()
                    elif provider == 'spotify-playlist':
                        print(f"[polling] Iniciando spotify-playlist con Playwright para {url}")
                        # Extract the playlist ID from the URL
                        playlist_id = None
                        m = re.search(r"spotify\.com/playlist/([A-Za-z0-9]+)", url)
                        if m:
                            playlist_id = m.group(1)
                        if playlist_id:
                            cb_url = source.get('callback_url')
                            cb_sec = source.get('callback_secret')
                            q_id = source.get('queue_id')
                            pl_name = source.get('pl_name', '')
                            # Since callback_url is provided, _ytdlp_chrome_cookies_extract will automatically run in background!
                            # Wait, the function itself spawns a background thread if callback_url is provided.
                            _ytdlp_chrome_cookies_extract(
                                playlist_id,
                                chat_id=chat_id,
                                bot_token=bot_token,
                                callback_url=cb_url,
                                callback_secret=cb_sec,
                                queue_id=q_id,
                                pl_name=pl_name
                            )
                        else:
                            success = False
                            print("[polling] Invalid Spotify URL")
                    else:
                        artist = task.get('artist', '')
                        title = task.get('title', '')
                        print(f"[polling] Descarga estandar para URL='{url}', artist='{artist}', title='{title}'")
                        try:
                            os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
                            os.chdir(DOWNLOAD_FOLDER)
                            if not url and artist and title:
                                # Track from Spotify Playlist without direct URL
                                # Limpiar el nombre del artista para Deezer (solo usar el primero)
                                primary_artist = artist.split(',')[0].strip()
                                exe = get_executable('rip')
                                search_query = f"{primary_artist} {title}".strip()
                                res = subprocess.run([exe, '--no-db', '--folder', DOWNLOAD_FOLDER, 'search', '-f', 'deezer', 'track', search_query], capture_output=True, text=True)
                                success = res.returncode == 0
                                if not success:
                                    print(f"[polling] Deezer search failed for {search_query}, falling back to yt-dlp")
                                    exe_yt = get_executable('yt-dlp')
                                    res_yt = subprocess.run([exe_yt, '-x', '--audio-format', 'mp3', f"ytsearch1:{search_query}"], capture_output=True, text=True)
                                    success = res_yt.returncode == 0
                            elif url and 'deezer.com' in url:
                                exe = get_executable('rip')
                                res = subprocess.run([exe, '--no-db', '--folder', DOWNLOAD_FOLDER, 'url', url], capture_output=True, text=True)
                                success = res.returncode == 0
                            elif url:
                                exe = get_executable('yt-dlp')
                                res = subprocess.run([exe, '-x', '--audio-format', 'mp3', url], capture_output=True, text=True)
                                success = res.returncode == 0
                            else:
                                success = False
                                print("[polling] Tarea sin URL ni metadata válida")
                        except Exception as e:
                            print(f"[polling] Download error: {e}", flush=True)
                            success = False
                            
                    status = 'completed' if success else 'error'
                    requests.patch(f"{API_QUEUE_URL}?secret={PROXY_SECRET}", json={"id": task['id'], "status": status}, timeout=10)
                    continue
        except Exception as e:
            pass
        time.sleep(10)


if __name__ == "__main__":
    print("🚀 ytdlp-proxy LOCAL arrancado en http://localhost:5001")
    print(f"   Secret: {PROXY_SECRET}")
    threading.Thread(target=_polling_daemon, daemon=True).start()
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
