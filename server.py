"""
Backend for the downloader page (also serves the page, manifest and icons).

POST /api/preview    {"url": "..."}                      -> {title, thumbnail, video_url}
POST /api/download   {"url": "...", "format": "mp3-320" | "mp4-1080"}  -> the file

Needs: Python 3.9+, ffmpeg, and `pip install -r requirements.txt`.
All files can sit in the repo root (no folders needed).
"""
import os
import shutil
import tempfile
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")
CORS(app, origins=os.environ.get("ALLOWED_ORIGIN", "*"))

ALLOWED_HOSTS = (
    "youtube.com", "youtu.be", "instagram.com", "facebook.com", "fb.watch", "fb.com",
    "pinterest.com", "pin.it", "twitter.com", "x.com",
)
MAX_BYTES = 500 * 1024 * 1024
DL_LIMIT = int(os.environ.get("RATE_LIMIT", 10))            # downloads per hour per visitor
PREVIEW_LIMIT = int(os.environ.get("PREVIEW_LIMIT", 40))    # previews per hour per visitor (0 = off)
_hits = {}

# Files the site serves from the repo root (or a static/ folder)
PUBLIC_FILES = {
    "index.html": "text/html",
    "manifest.json": "application/manifest+json",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}


def limited(kind, limit):
    if limit <= 0:
        return False
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()
    key = (kind, ip)
    now = time.time()
    recent = [t for t in _hits.get(key, []) if now - t < 3600]
    if len(recent) >= limit:
        _hits[key] = recent
        return True
    recent.append(now)
    _hits[key] = recent
    return False


def host_allowed(url):
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def download_options(fmt, outdir):
    base = {
        "outtmpl": os.path.join(outdir, "%(title).80s.%(ext)s"),
        "noplaylist": True, "quiet": True,
        "max_filesize": MAX_BYTES, "restrictfilenames": True,
    }
    if fmt == "mp3-320":
        base.update(format="bestaudio/best", postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}])
    else:
        base.update(format="bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
                    merge_output_format="mp4")
    return base


@app.post("/api/preview")
def preview():
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not host_allowed(url):
        return jsonify(error="This site isn't supported."), 400
    if limited("preview", PREVIEW_LIMIT):
        return jsonify(error="Too many previews. Try again later."), 429
    opts = {
        "quiet": True, "noplaylist": True, "skip_download": True, "socket_timeout": 15,
        "format": "best[ext=mp4][protocol^=http]/best[protocol^=http]/best",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("entries"):
            entries = [e for e in info["entries"] if e]
            info = entries[0] if entries else info
        direct = info.get("url") if str(info.get("protocol", "")).startswith("http") else None
        return jsonify(
            title=(info.get("title") or "")[:120],
            thumbnail=info.get("thumbnail"),
            video_url=direct,
            duration=info.get("duration"),
        )
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500


@app.post("/api/download")
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    fmt = data.get("format")
    if fmt not in ("mp3-320", "mp4-1080"):
        return jsonify(error="Unknown format."), 400
    if not host_allowed(url):
        return jsonify(error="This site isn't supported."), 400
    if limited("download", DL_LIMIT):
        return jsonify(error="Too many downloads. Try again in an hour."), 429

    outdir = tempfile.mkdtemp(prefix="dl_")
    try:
        with yt_dlp.YoutubeDL(download_options(fmt, outdir)) as ydl:
            ydl.download([url])
        files = os.listdir(outdir)
        if not files:
            raise RuntimeError("Nothing was downloaded (file may be too large or private).")
        resp = send_file(os.path.join(outdir, files[0]), as_attachment=True, download_name=files[0])
        resp.call_on_close(lambda: shutil.rmtree(outdir, ignore_errors=True))
        return resp
    except Exception as e:
        shutil.rmtree(outdir, ignore_errors=True)
        return jsonify(error=str(e)[:300]), 500


def serve_public(name):
    for d in (BASE, os.path.join(BASE, "static")):
        if os.path.exists(os.path.join(d, name)):
            return send_from_directory(d, name, mimetype=PUBLIC_FILES[name])
    return jsonify(error=name + " not found"), 404


@app.get("/")
def index():
    return serve_public("index.html")


@app.get("/<name>")
def public_file(name):
    if name in PUBLIC_FILES:
        return serve_public(name)
    return jsonify(error="Not found"), 404


@app.get("/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
