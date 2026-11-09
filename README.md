# youtube-downloader

Dockerized HTTP API around [yt-dlp](https://github.com/yt-dlp/yt-dlp) for fetching
video metadata and downloading media into a configurable data directory.

YouTube requires EJS (External JS Scripts) for challenge solving and PO Tokens
(Proof of Origin) to bypass bot detection. Both are handled automatically in the
Docker image via Deno and the
[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
plugin.

> Published to CasaOS through the separate **casaos-store** repository, which
> builds this app from its git URL.

## Endpoints

| Method | Path                          | Description                                  |
| ------ | ----------------------------- | -------------------------------------------- |
| GET    | `/`                           | Health check + yt-dlp version + PO token provider status |
| POST   | `/download/{id}`              | Download the requested parts for a video in **a single call** |
| GET    | `/{id}`                       | List which parts are already stored for a video |
| GET    | `/{id}/{part}`                | Serve a stored part file directly (streamed inline by default, `?download=1` to force) |
| DELETE | `/{id}`                       | Delete **everything** for a video at once    |
| DELETE | `/{id}/{part}`                | Delete a single part                         |

All endpoints take a **bare YouTube video ID** (`/download/dQw4w9WgXcQ`).

`{part}` is one of: `video`, `audio`, `cover`, `transcript`, `info`.

### Directory structure

Each video owns its folder; the file extensions are always the same:

```
$DATA_DIR/
└── <video_id>/
    ├── video.m4v      # video (mp4/avc1 container, renamed .m4v)
    ├── audio.m4a      # audio only (AAC in m4a)
    ├── cover.jpg      # thumbnail, standalone cover art
    ├── transcript.txt # single-line plain-text transcript
    └── info.json      # full yt-dlp metadata
```

### POST /download/{id}

Downloads several parts in **one call**. Already-present files are skipped
unless `?refresh=1`. Each part is isolated, so a failure on one (e.g. no
subtitles) does **not** affect the others and is reported in the response.

| Field            | Type     | Default        | Notes                                  |
| ---------------- | -------- | -------------- | -------------------------------------- |
| `parts`          | `string[]` | all five     | Which parts to fetch                   |
| `video_quality`  | `string` | `bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]` | Raw yt-dlp format selector for the video |
| `audio_quality`  | `string` | `bestaudio[ext=m4a]/bestaudio` | Raw yt-dlp format selector for the audio |

> Passing quality selectors as JSON body means no more URL-encoding pain
> (`+`, `[]`, `^` … are sent verbatim).

```bash
# Download everything for a video
curl -X POST localhost:8000/download/dQw4w9WgXcQ \
  -H 'Content-Type: application/json' -d '{}'

# Only video + cover, custom resolution
curl -X POST localhost:8000/download/dQw4w9WgXcQ \
  -H 'Content-Type: application/json' \
  -d '{"parts":["video","cover"],"video_quality":"bestvideo[height<=1080]+bestaudio"}'

# Force a re-download even if files exist
curl -X POST "localhost:8000/download/dQw4w9WgXcQ?refresh=1" \
  -H 'Content-Type: application/json' -d '{}'
```

Response — per-part `status` is `downloaded`, `skipped` or `error`:

```json
{
  "id": "dQw4w9WgXcQ",
  "title": "Rick Astley - Never Gonna Give You Up",
  "requested": ["video", "audio", "cover", "transcript", "info"],
  "parts": {
    "video":      {"status": "downloaded", "path": "dQw4w9WgXcQ/video.m4v", "size_bytes": 10485760},
    "audio":      {"status": "skipped",    "path": "dQw4w9WgXcQ/audio.m4a", "size_bytes": 3048832},
    "cover":      {"status": "downloaded", "path": "dQw4w9WgXcQ/cover.jpg",  "size_bytes": 154321},
    "transcript": {"status": "error", "error": "No subtitles found"},
    "info":       {"status": "downloaded", "path": "dQw4w9WgXcQ/info.json", "size_bytes": 88213}
  }
}
```

### GET /{id}

State of a video folder without downloading anything:

```bash
curl localhost:8000/dQw4w9WgXcQ
# {"id":"dQw4w9WgXcQ","title":"...","parts":{"video":{"present":true,...},...}}
```

### GET /{id}/{part}

Serves the file directly with the right `Content-Type`
(`video/mp4`, `audio/mp4`, `image/jpeg`, `text/plain`, `application/json`).
Video/audio/cover are **streamed inline by default** (`Range` requests are
supported, so seeking/scrubbing works in players). Append `?download=1` to force
an attachment download. Returns `404` if the part has not been downloaded yet.

```bash
# stream/read inline (open in a player/browser)
curl localhost:8000/dQw4w9WgXcQ/video        # video stream (m4v)
curl localhost:8000/dQw4w9WgXcQ/audio        # audio stream (m4a)
curl localhost:8000/dQw4w9WgXcQ/cover        # image
curl localhost:8000/dQw4w9WgXcQ/transcript   # plain text
curl localhost:8000/dQw4w9WgXcQ/info         # full JSON metadata

# force a download
curl -o video.m4v "localhost:8000/dQw4w9WgXcQ/video?download=1"
curl -o audio.m4a "localhost:8000/dQw4w9WgXcQ/audio?download=1"
```

### DELETE /{id} and DELETE /{id}/{part}

Removes the whole video folder (all parts at once) or a single part.

```bash
curl -X DELETE localhost:8000/dQw4w9WgXcQ
# {"id":"dQw4w9WgXcQ","deleted":["video","audio","cover","transcript","info"]}
curl -X DELETE localhost:8000/dQw4w9WgXcQ/cover
# {"id":"dQw4w9WgXcQ","part":"cover","deleted":true}
```

## Configuration

| Env var        | Default   | Description                                  |
| -------------- | --------- | -------------------------------------------- |
| `DATA_DIR`     | `./data`  | Base directory for all downloads             |
| `PORT`         | `8000`    | uvicorn listen port                          |
| `YTDLP_UPDATE` | `1`       | Run `yt-dlp -U` at startup to self-update    |

## Run locally

```bash
docker build -t yt-dlp-api:latest .
mkdir -p data
docker run -d --name yt-dlp-api \
  -p 8000:8000 \
  -e DATA_DIR=/data \
  -v "$(pwd)/data:/data" \
  yt-dlp-api:latest
```

Or with the compose file:

```bash
docker compose up -d --build
```

Then:

```bash
curl localhost:8000/
curl -X POST localhost:8000/download/dQw4w9WgXcQ -H 'Content-Type: application/json' -d '{}'
curl localhost:8000/dQw4w9WgXcQ
curl -o cover.jpg localhost:8000/dQw4w9WgXcQ/cover
ls data/dQw4w9WgXcQ/
curl -X DELETE localhost:8000/dQw4w9WgXcQ
```

Interactive API docs: http://localhost:8000/docs

## Notes

- **Up-to-date yt-dlp**: the image downloads the standalone yt-dlp binary
  (not pip), so `yt-dlp -U` at startup keeps it current without rebuilds.
- **EJS (External JS Scripts)**: YouTube now requires solving JavaScript
  challenges. [Deno](https://deno.com) is installed as the JS runtime, and yt-dlp
  is configured with `--js-runtimes deno` + `--remote-components ejs:github`.
- **PO Tokens (Proof of Origin)**: YouTube enforces BotGuard attestation tokens.
  The [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
  plugin runs an HTTP server on port 4416 inside the container, generating PO
  tokens automatically via BgUtils. The health endpoint reports
  `pot_provider: "ok" | "unavailable"`.
- **ffmpeg** is bundled for format merging and thumbnail→JPEG conversion.
- `POST /download` runs a **single metadata extraction** per call and reuses it
  for every requested part. Video and audio downloads run in their own yt-dlp
  subprocess each; the transcript uses a separate subtitle extraction targeting
  the video's original language (uploaded subtitles when available, otherwise
  auto-generated ones).
- Cover art is **not** embedded into the media files anymore: it is stored as a
  standalone `cover.jpg` next to the media.
- The video container is mp4/avc1 (as selected by default); the produced file is
  simply renamed `.m4v` for clarity.
- The API shells out to the `yt-dlp` binary (subprocess) rather than importing
  the Python module, so the binary is the single source of truth.
