Downloader app (page + server in one)

RUN ON YOUR COMPUTER (needs Docker):
  docker build -t downloader .
  docker run -p 5000:5000 downloader
  Open http://localhost:5000

RUN WITHOUT DOCKER (needs Python 3.9+ and ffmpeg):
  pip install -r requirements.txt
  python server.py
  Open http://localhost:5000

PUT ONLINE (Render.com, free tier works):
  1. Upload this folder to a GitHub repo.
  2. On Render: New > Web Service > pick the repo > Runtime: Docker > Deploy.
  3. Open the URL Render gives you. Done.

Settings (optional environment variables):
  RATE_LIMIT=10   downloads per hour per visitor (0 = off)
