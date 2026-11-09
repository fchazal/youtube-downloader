# AGENT.md — youtube-downloader

Everything needed to resume work on this project. Read this first.

## Overview

A Dockerized HTTP API around [yt-dlp](https://github.com/yt-dlp/yt-dlp) that lets
you fetch metadata, download video/audio/cover/transcript/info **per video**, and
delete them all at once. Files are grouped in one folder per video under a
configurable data directory and served directly over HTTP.

YouTube requires EJS (External JS Scripts) for challenge solving and PO Tokens
(Proof of Origin) to avoid bot detection. Both are handled automatically via
[Deno](https://deno.com) and the
[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
plugin running an HTTP server on port 4416 inside the container.

It is published as the **`youtube`** app of the CasaOS store
(`/Users/fchazal/Developer/casaos-store`), which builds the image on the server
from this repo's git URL.

## Tech stack

- Python 3.12 + **FastAPI** + **uvicorn** (synchronous endpoints; yt-dlp runs in a subprocess)
- **yt-dlp standalone binary** (installed in the image, self-updates at startup)
- **Deno** JS runtime (EJS challenge solving + bgutil PO token server)
- **bgutil-ytdlp-pot-provider** (PO Token generation via BotGuard attestation)
- **ffmpeg** (format merging, thumbnail→JPEG conversion)
- Docker (multi-stage build), Docker Compose for local dev

## Repo structure

```
youtube-downloader/
├── main.py              # FastAPI app: health, download, items
├── Dockerfile           # multi-stage: ffmpeg + yt-dlp binary + Python deps + Deno + bgutil
├── requirements.txt     # fastapi, uvicorn[standard], python-multipart
├── docker-compose.yml   # local dev (build: ., port 8000, ./data:/data)
├── scripts/publish.sh   # build + push image to a registry
├── .dockerignore
├── .gitignore
├── README.md            # user-facing docs
└── AGENT.md             # this file
```

Remote: `https://github.com/fchazal/youtube-downloader.git` (branch `main`).
Current version: `2.0.0` (FastAPI app version + image tag `yt-dlp-api:latest`).

## How it works

- The API **shells out to the `yt-dlp` binary** via `subprocess.run` (not the
  Python module). This keeps the binary the single source of truth and lets it
  self-update with `yt-dlp -U`.
- **EJS (External JS Scripts)**: YouTube now requires solving JavaScript challenges
  for video extraction. Deno is installed in the container as the JS runtime, and
  yt-dlp is configured with `--js-runtimes deno` + `--remote-components ejs:github`
  (fallback). EJS scripts are bundled with the yt-dlp standalone binary.
- **PO Tokens (Proof of Origin)**: YouTube enforces BotGuard attestation tokens to
  block automated access. The
  [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
  plugin is installed in `~/.config/yt-dlp/plugins/` and connects to an HTTP server
  (`http://127.0.0.1:4416`) running inside the container. The `--extractor-args
  "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416"` flag is set in
  `/etc/yt-dlp.conf`. The health endpoint reports `pot_provider: "ok" | "unavailable"`.
- **Routes accept only bare video IDs** (`{video_id}`). `sanitize_id()` validates
  against `^[A-Za-z0-9_-]+$` before any path usage (folder name / URL build);
  `normalize_target()` wraps the ID into `https://www.youtube.com/watch?v=<id>`.
- **Per-video folder layout** under `$DATA_DIR`:
  ```
  $DATA_DIR/<video_id>/
  ├── video.m4v       # video (mp4/avc1; produced as .mp4 then os.replace → .m4v)
  ├── audio.m4a       # audio only (--extract-audio --audio-format m4a)
  ├── cover.jpg       # standalone thumbnail (ffmpeg convert of info.thumbnail)
  ├── transcript.txt  # single-line plain text
  └── info.json       # full yt-dlp metadata dump
  ```
  The five parts are declared in `PARTS`; `FILE_NAMES`/`MIME_TYPES` map each
  part to its file name and served content type.
- `run_ytdlp()` has a 3600 s timeout and captures output; errors are surfaced as
  HTTP errors (404 for unknown videos, 502 for download failures).
- **`POST /download/{id}`** runs a **single `get_info_json()`** (one `yt-dlp -J`
  extraction) reused by every requested part, then dispatches to per-part
  downloaders. Video/audio each run their own download subprocess; transcript runs
  a separate subtitle extraction (`--skip-download --sub-format ttml/vtt/best
  --sub-langs <original-language> --write-subs` for uploaded tracks or
  `--write-auto-subs` for generated ones, HTML/timestamp stripped, joined to one
  line). The original language is resolved from `info.language`, `<lang>-orig`
  auto-caption keys, and audio-format `language`/`language_preference` metadata;
  yt-dlp no longer always exposes `-orig`, so the old `.*orig`-only selector is
  kept only as a fallback.
  Each part runs in its own `try/except`, so one failure (e.g. no subtitles) is
  reported per-part and never breaks the others. Idempotent by default: existing
  parts are `skipped` unless `?refresh=1`.
- **Quality selectors** are passed as JSON body fields (`video_quality`,
  `audio_quality`) — raw yt-dlp `--format` selectors, so `+`/`[]`/`^` need no
  URL encoding anymore. Defaults match v1 (`bestvideo[ext=mp4][vcodec^=avc1]+
  bestaudio[ext=m4a]/best[ext=mp4]` and `bestaudio[ext=m4a]/bestaudio`).
- **No cover-art embedding anymore**: media files are not re-muxed with ffmpeg
  after download (v1 embedded the thumbnail + title/network/date metadata).
  The cover is a standalone `cover.jpg` only.
- Files are served directly (`FileResponse`) with proper media types; media is
  **streamed inline by default** (`content_disposition_type="inline"`, Starlette
  handles `Range`/206 so players can seek). `?download=1` switches to
  `attachment` with a filename. There is **no WebDAV** anymore.

## Endpoints

| Method | Path                    | Description                          |
| ------ | ----------------------- | ------------------------------------ |
| GET    | `/`                     | Health + yt-dlp version + data_dir + pot_provider status |
| POST   | `/download/{id}`        | Download parts (default: all 5). Body `{"parts":[...], "video_quality":..., "audio_quality":...}`. Query `?refresh=1` forces re-download |
| GET    | `/{id}`                 | List which parts exist for a video   |
| GET    | `/{id}/{part}`          | Serve the part file (streamed inline by default; `?download=1` forces attachment) |
| DELETE | `/{id}`                 | Delete the whole video folder (all parts) |
| DELETE | `/{id}/{part}`          | Delete one part                      |
| GET    | `/docs`                 | Interactive OpenAPI UI               |

`{part}` ∈ `video | audio | cover | transcript | info`. Per-part response
`status` ∈ `downloaded | skipped | error`.

## Configuration (env vars)

| Env var        | Default   | Notes                                      |
| -------------- | --------- | ------------------------------------------ |
| `DATA_DIR`     | `./data`  | Base dir for downloads (default is the code default; image sets `/data`) |
| `PORT`         | `8000`    | uvicorn listen port                        |
| `YTDLP_UPDATE` | `1`       | If `1`, runs `yt-dlp -U` at startup        |

## Build & run

```bash
# Docker image
docker build -t yt-dlp-api:latest .

# Compose (local dev)
docker compose up -d --build

# Plain docker run
mkdir -p data
docker run -d --name yt-dlp-api -p 8000:8000 \
  -e DATA_DIR=/data -v "$(pwd)/data:/data" yt-dlp-api:latest
```

The image has `VOLUME ["/data"]` and the container default `DATA_DIR=/data`.
Outside the container, the Python default is `./data` (relative to cwd).

### Local (no Docker)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# needs the yt-dlp binary + ffmpeg on PATH
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Smoke test

```bash
curl localhost:8000/                                  # health + version + pot_provider
curl -X POST localhost:8000/download/dQw4w9WgXcQ \
  -H 'Content-Type: application/json' -d '{}'         # download everything
curl localhost:8000/dQw4w9WgXcQ                       # state of the folder
curl -o video.m4v localhost:8000/dQw4w9WgXcQ/video
curl localhost:8000/dQw4w9WgXcQ/info                  # metadata JSON
curl -X DELETE localhost:8000/dQw4w9WgXcQ             # delete everything
ls data/                                              # folder gone
```

## Publishing

### To a container registry

```bash
./scripts/publish.sh ghcr.io/<your-user>     # builds + pushes yt-dlp-api:latest
```

### CasaOS store integration

- The CasaOS store lives at `/Users/fchazal/Developer/casaos-store` (repo
  `https://github.com/fchazal/casaos-store.git`).
- `Apps/youtube/docker-compose.yml` builds this app **from this repo's git URL**
  (`build.context: https://github.com/fchazal/youtube-downloader.git`), so the
  CasaOS server builds the image itself — no registry needed. If the app repo
  moves, update that `build.context`.
- Store URL used in CasaOS (must be the archive, not the plain `.git` URL):
  `https://github.com/fchazal/casaos-store/archive/refs/heads/main.zip`
  (CasaOS's `go-getter` does not treat a plain `.git` URL as a git repo).
- To update what CasaOS serves: commit + **push** both repos, then in CasaOS
  **remove and re-add** the store (CasaOS caches the catalog; GitHub archives
  return no `Content-Length`, so in-place updates are skipped).

## Gotchas / decisions to keep in mind

- **yt-dlp must be the binary, not the pip module** — `main.py` imports only
  `subprocess` for it. Do not `import yt_dlp`.
- yt-dlp self-update via `pip` fails; the standalone binary (`yt-dlp -U`) is
  what works. That's why the Dockerfile downloads the binary with curl.
- `video_quality`/`audio_quality` are **raw yt-dlp format selectors** passed
  straight to `--format`. They arrive in the JSON body, so no `+`→space
  decoding issue. If a selector matches nothing, yt-dlp errors → that part gets
  `status: error`.
- **Video**: produced as mp4 (default selector keeps `[vcodec^=avc1][ext=mp4]`),
  then the produced file is renamed to `video.m4v` via `os.replace`. The m4v
  container is identical to mp4.
- **Audio**: produced as m4a via `--extract-audio --audio-format m4a`, renamed
  to `audio.m4a`.
- **Cover**: `info.thumbnail` is downloaded and converted to `cover.jpg` with a
  one-frame ffmpeg pass. There is **no re-mux/embed step** on the media files.
- **Refresh semantics**: skip existing part unless `refresh=1`. `run_download()`
  diffs the output directory before/after, so an existing canonical file
  (`video.m4v`) is not mistaken for a fresh download; then `os.replace` swaps in
  the new file atomically.
- **Failure isolation**: each part is wrapped in try/except inside `/download`;
  `HTTPException.detail` becomes the part `error`. A bad part never 500s the call.
- **Route ordering matters**: FastAPI's default routes (`/docs`, `/openapi.json`,
  `/redoc`) are registered first and always win. `GET /{video_id}` (list),
  `GET /{video_id}/{part}` (serve), `DELETE /{video_id}` and
  `DELETE /{video_id}/{part}` are generic. Keep their relative order (list before
  part routes) and do not add new literal GET/DELETE routes after them, or the
  generic single/two-segment routes would capture those paths.
- **Path safety**: `sanitize_id()` (regex `^[A-Za-z0-9_-]+$`) guards every use of
  `{video_id}` as a filesystem folder under `DATA_DIR`. Do not bypass it.
- `POST /download` runs one metadata extraction even for a transcript-only
  request (the transcript needs the folder id/title too). That means a fully
  blocked video yields 404 even if you only wanted subtitles — acceptable tradeoff.
- **wsgidav was removed in v2** — `/dav` no longer exists. Requirements are just
  fastapi, uvicorn, python-multipart. Old v1 subdirectories
  (`videos/`, `audios/`, `transcripts/`) from a previous data dir are **not**
  migrated; delete them by hand if present.
- `data/` is gitignored; a fresh `DATA_DIR` is auto-created at import time.
- Container stop behavior is fine (tested ~1 s); the `sh -c` CMD works, but if
  slow stops ever appear, switch to `exec uvicorn …` or a tini entrypoint.

## Current status & possible next steps

Done: v2 API — one-folder-per-video layout (`video.m4v`/`audio.m4a`/
`cover.jpg`/`transcript.txt`/`info.json`), `POST /download/{id}` (single metadata
extraction, all parts, idempotent, `?refresh=1`), `GET /{id}` (state),
`GET /{id}/{part}` file serving (streamed inline, `?download=1`),
`DELETE /{id}` (whole folder) and
per-part delete, quality selectors in JSON body, cover as standalone JPEG
(embedding removed), WebDAV removed.

Possible next steps (not started): async downloads with job status, web UI,
playlist export, per-request rate limiting, authentication, tests
(no test suite exists yet — `main.py` has no unit tests).
