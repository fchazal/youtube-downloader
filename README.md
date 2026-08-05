# youtube-downloader

Dockerized HTTP API around [yt-dlp](https://github.com/yt-dlp/yt-dlp) for fetching
video metadata and downloading media into a configurable data directory.

> Published to CasaOS through the separate **casaos-store** repository, which
> builds this app from its git URL.

## Endpoints

| Method | Path                    | Description                                  |
| ------ | ----------------------- | -------------------------------------------- |
| GET    | `/`                     | Health check + yt-dlp version                |
| GET    | `/info/{url_or_id}`     | Full metadata/properties for a URL or ID     |
| GET    | `/formats/{url_or_id}`  | List of available formats/qualities          |
| POST   | `/download`             | Download video / audio / subtitles           |

Accepts a full URL (`/info/https://www.youtube.com/watch?v=...`) or a bare video
ID (`/info/dQw4w9WgXcQ`).

### POST /download

```json
{
  "url_or_id": "dQw4w9WgXcQ",
  "subdir": "music",
  "audio_only": true,
  "video_quality": 1080,
  "video_ext": "mp4",
  "audio_format": "mp3",
  "audio_quality": 192,
  "write_subtitles": true,
  "subtitle_langs": ["en"],
  "write_auto_subs": true,
  "embed_subtitles": false,
  "playlist": false
}
```

Downloads land in a **subdirectory** of `$DATA_DIR`. Response:
`{ "ok": true, "directory": "/data/<subdir>", "files": [...] }`

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
curl localhost:8000/info/dQw4w9WgXcQ
curl -X POST localhost:8000/download -H 'Content-Type: application/json' \
  -d '{"url_or_id": "dQw4w9WgXcQ", "subdir": "music", "audio_only": true}'
```

Interactive API docs: http://localhost:8000/docs

## Notes

- **Up-to-date yt-dlp**: the image downloads the standalone yt-dlp binary
  (not pip), so `yt-dlp -U` at startup keeps it current without rebuilds.
- **ffmpeg** is bundled for audio extraction, format merging, subtitle embedding.
- The API shells out to the `yt-dlp` binary (subprocess) rather than importing
  the Python module, so the binary is the single source of truth.
