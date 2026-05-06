# Navidrome → ListenBrainz Loved Tracks Sync

Syncs loved/starred tracks from your Navidrome database to your ListenBrainz profile. Runs as a Docker container, either as a one-shot sync or on a cron schedule.

## How it works

- **Full diff sync** — fetches all loved tracks from ListenBrainz, compares against all starred tracks in Navidrome, and submits only the delta. Safe to re-run; already-synced tracks are skipped.
- **Incremental sync** — on cron ticks after the first run, only tracks starred since the last sync are submitted. No ListenBrainz read needed.
- Container restart always triggers a fresh full diff sync.

## Requirements

Each track must have a valid `mbz_recording_id` (MusicBrainz Recording ID) in Navidrome. Tracks without one are skipped silently and reported in the startup summary.

To tag your library:
1. Tag files with [MusicBrainz Picard](https://picard.musicbrainz.org/)
2. Run a full rescan in Navidrome so the IDs are written to the database

## Setup

Copy `docker-compose.example.yml` and configure the environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `LISTENBRAINZ_TOKEN` | Yes | — | Your ListenBrainz user token |
| `LISTENBRAINZ_USER` | Yes | — | Your ListenBrainz username |
| `NAVIDROME_DB_PATH` | No | `/data/navidrome.db` | Path to Navidrome's SQLite database inside the container |
| `CRON_SCHEDULE` | No | — | 5-field cron expression. If unset, runs once and exits. |
| `TZ` | No | `UTC` | Timezone for the cron schedule (e.g. `America/New_York`) |
| `RUN_ON_START` | No | `false` | Run a full sync immediately on startup before the first cron tick. Only applies when `CRON_SCHEDULE` is set. |

Mount your Navidrome data directory to `/data` (read-only).

## Run once

```bash
docker run --rm \
  -e LISTENBRAINZ_TOKEN=your_token \
  -e LISTENBRAINZ_USER=your_username \
  -v /path/to/navidrome/data:/data:ro \
  -v $(pwd):/app \
  -w /app \
  python:3.10-slim \
  bash -c "pip install -q -r requirements.txt && python love_tracks_listenbrainz.py"
```

## Run on a schedule (Docker Compose)

See `docker-compose.example.yml` for a full example. The key section:

```yaml
environment:
  - LISTENBRAINZ_TOKEN=your_token
  - LISTENBRAINZ_USER=your_username
  - CRON_SCHEDULE=30 1 * * *   # 1:30 AM daily
  - TZ=America/New_York
  - RUN_ON_START=true
volumes:
  - /path/to/navidrome/data:/data:ro
  - /path/to/this/repo:/app
```
