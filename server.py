"""
Backend for the downloader page (also serves the page, manifest and icons).

POST /api/preview        {"url": "..."}  -> {title, thumbnail}
POST /api/preview-video  {"url": "..."}  -> {video_url} (small mp4 copy for playing)
POST /api/download   {"url": "...", "format": "audio" | "video", "quality": "best" | "720" | "320" ...}  -> the file

Needs: Python 3.9+, ffmpeg, and `pip install -r requirements.txt`.
All files can sit in the repo root (no folders needed).
"""
import glob
import hashlib
import os
import re
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
PV_LIMIT = int(os.environ.get("PV_LIMIT", 20))              # video previews per hour per visitor
PV_MAX_BYTES = 80 * 1024 * 1024                              # biggest preview file kept
PV_TTL = 20 * 60                                             # preview files are deleted after 20 min
PV_DIR = os.path.join(tempfile.gettempdir(), "pv_cache")
os.makedirs(PV_DIR, exist_ok=True)
_hits = {}

# Files the site serves from the repo root (or a static/ folder)
PUBLIC_FILES = {
    "index.html": "text/html",
    "landing.html": "text/html",
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


KINDS = {"audio": "audio", "video": "video", "audio-original": "audio",
         "video-original": "video", "mp3-320": "audio", "mp4-1080": "video"}
MP3_RATES = ("320", "192", "128")


def download_options(kind, quality, outdir):
    """quality: "best" = original (no re-encode); video: a number = highest resolution up to it; audio: mp3 bitrate."""
    base = {
        "outtmpl": os.path.join(outdir, "%(title).80s.%(ext)s"),
        "noplaylist": True, "quiet": True,
        "max_filesize": MAX_BYTES, "restrictfilenames": True,
    }
    if kind == "audio":
        if quality in MP3_RATES:
            pp = {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality}
        else:
            pp = {"key": "FFmpegExtractAudio", "preferredcodec": "best"}
        base.update(format="bestaudio/best", postprocessors=[pp])
    else:
        base.update(format="bv*+ba/b", merge_output_format="mp4/mkv")
        if quality.isdigit():
            base["format_sort"] = ["res:" + quality]
    return base


@app.post("/api/preview")
def preview():
    """Fast: title + thumbnail only."""
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not host_allowed(url):
        return jsonify(error="This site isn't supported."), 400
    if limited("preview", PREVIEW_LIMIT):
        return jsonify(error="Too many previews. Try again later."), 429
    opts = {"quiet": True, "noplaylist": True, "skip_download": True,
            "socket_timeout": 15, "ignore_no_formats_error": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("entries"):
            entries = [e for e in info["entries"] if e]
            info = entries[0] if entries else info
        res = set()
        for f in info.get("formats") or []:
            if f.get("vcodec") == "none":
                continue
            w, h = f.get("width"), f.get("height")
            r = min(w, h) if w and h else h
            if r:
                res.add(int(r))
        return jsonify(title=(info.get("title") or "")[:120], thumbnail=info.get("thumbnail"),
                       duration=info.get("duration"), heights=sorted(res, reverse=True))
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500


def clean_old_previews():
    now = time.time()
    for f in glob.glob(os.path.join(PV_DIR, "*")):
        try:
            if now - os.path.getmtime(f) > PV_TTL:
                os.remove(f)
        except OSError:
            pass


def preview_file_for(pid):
    pat = re.escape(pid) + r"\.(mp4|webm|mov|m4v)"
    for f in glob.glob(os.path.join(PV_DIR, pid + ".*")):
        if re.fullmatch(pat, os.path.basename(f)):
            return os.path.basename(f)
    return None


@app.post("/api/preview-video")
def preview_video():
    """Makes a small (up to 480p) mp4 copy so ANY ratio/format plays in the browser."""
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not host_allowed(url):
        return jsonify(error="This site isn't supported."), 400
    if limited("pvid", PV_LIMIT):
        return jsonify(error="Too many previews. Try again later."), 429
    clean_old_previews()
    pid = hashlib.sha1(url.encode()).hexdigest()[:16]
    name = preview_file_for(pid)
    if not name:
        opts = {
            "outtmpl": os.path.join(PV_DIR, pid + ".%(ext)s"),
            "noplaylist": True, "quiet": True, "socket_timeout": 20,
            "format": "bv*+ba/b",
            "format_sort": ["res:480", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
            "merge_output_format": "mp4",
            "max_filesize": PV_MAX_BYTES,
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e:
            return jsonify(error=str(e)[:200]), 500
        name = preview_file_for(pid)
    if not name:
        return jsonify(error="Video is too large or not a video"), 413
    return jsonify(video_url="/api/pv/" + name)


@app.get("/api/pv/<name>")
def preview_file(name):
    if not re.fullmatch(r"[a-f0-9]{16}\.(mp4|webm|mov|m4v)", name):
        return jsonify(error="Not found"), 404
    path = os.path.join(PV_DIR, name)
    if not os.path.exists(path):
        return jsonify(error="Preview expired. Tap Go again."), 404
    return send_file(path, mimetype="video/webm" if name.endswith(".webm") else "video/mp4", conditional=True)


@app.post("/api/download")
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    kind = KINDS.get(data.get("format"))
    quality = str(data.get("quality") or "best")
    if not kind:
        return jsonify(error="Unknown format."), 400
    ok_quality = quality in MP3_RATES + ("best",) if kind == "audio" else re.fullmatch(r"best|\d{3,4}", quality)
    if not ok_quality:
        return jsonify(error="Unknown quality."), 400
    if not host_allowed(url):
        return jsonify(error="This site isn't supported."), 400
    if limited("download", DL_LIMIT):
        return jsonify(error="Too many downloads. Try again in an hour."), 429

    outdir = tempfile.mkdtemp(prefix="dl_")
    try:
        with yt_dlp.YoutubeDL(download_options(kind, quality, outdir)) as ydl:
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
    for d in (BASE, os.path.join(BASE, "static")):
        if os.path.exists(os.path.join(d, "landing.html")):
            return serve_public("landing.html")
    return serve_public("index.html")


@app.get("/app")
def app_page():
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
