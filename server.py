"""
Backend for the downloader page.

POST /api/download   body: {"url": "...", "format": "mp3-320" | "mp4-1080"}
Returns the file itself (the page saves it), or {"error": "..."} with a 4xx/5xx status.

Needs: Python 3.9+, ffmpeg installed on the machine, and `pip install -r requirements.txt`.
"""
import os
import shutil
import tempfile
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp

app = Flask(__name__, static_folder="static", static_url_path="/static")
# In production set ALLOWED_ORIGIN to your site, e.g. https://mysite.com
CORS(app, origins=os.environ.get("ALLOWED_ORIGIN", "*"))

ALLOWED_HOSTS = (
    "youtube.com", "youtu.be",
    "instagram.com",
    "facebook.com", "fb.watch", "fb.com",
    "pinterest.com", "pin.it",
    "twitter.com", "x.com",
)
MAX_BYTES = 500 * 1024 * 1024  # refuse files above 500 MB

# Simple per-IP limit: RATE_LIMIT downloads per hour (set RATE_LIMIT=0 to turn off)
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", 10))
_hits = {}


def too_many_requests():
    if RATE_LIMIT <= 0:
        return False
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()
    now = time.time()
    recent = [t for t in _hits.get(ip, []) if now - t < 3600]
    if len(recent) >= RATE_LIMIT:
        _hits[ip] = recent
        return True
    recent.append(now)
    _hits[ip] = recent
    return False


def host_allowed(url):
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and any(
        host == h or host.endswith("." + h) for h in ALLOWED_HOSTS
    )


def options_for(fmt, outdir):
    base = {
        "outtmpl": os.path.join(outdir, "%(title).80s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "max_filesize": MAX_BYTES,
        "restrictfilenames": True,
    }
    if fmt == "mp3-320":
        base.update(
            format="bestaudio/best",
            postprocessors=[{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }],
        )
    else:  # mp4-1080
        base.update(
            format="bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            merge_output_format="mp4",
        )
    return base


@app.post("/api/download")
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    fmt = data.get("format")

    if fmt not in ("mp3-320", "mp4-1080"):
        return jsonify(error="Unknown format."), 400
    if not host_allowed(url):
        return jsonify(error="This site isn't supported."), 400
    if too_many_requests():
        return jsonify(error="Too many downloads. Try again in an hour."), 429

    outdir = tempfile.mkdtemp(prefix="dl_")
    try:
        with yt_dlp.YoutubeDL(options_for(fmt, outdir)) as ydl:
            ydl.download([url])
        files = os.listdir(outdir)
        if not files:
            raise RuntimeError("Nothing was downloaded (file may be too large or private).")
        path = os.path.join(outdir, files[0])
        resp = send_file(path, as_attachment=True, download_name=files[0])
        resp.call_on_close(lambda: shutil.rmtree(outdir, ignore_errors=True))
        return resp
    except Exception as e:
        shutil.rmtree(outdir, ignore_errors=True)
        return jsonify(error=str(e)[:300]), 500


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
