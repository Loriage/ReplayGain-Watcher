# ReplayGain Watcher

[![Docker](https://img.shields.io/badge/docker-ghcr.io%2Floriage%2Freplaygain--watcher-blue?logo=docker&logoColor=white)](https://github.com/Loriage/ReplayGain-Watcher/pkgs/container/replaygain-watcher)
[![Downloads](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fghcr-badge.elias.eu.org%2Fapi%2Floriage%2Freplaygain-watcher%2Freplaygain-watcher&query=downloadCount&label=downloads&logo=docker&color=2496ed)](https://github.com/Loriage/ReplayGain-Watcher/pkgs/container/replaygain-watcher)
![GitHub Release](https://img.shields.io/github/v/release/Loriage/ReplayGain-Watcher)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Build](https://img.shields.io/github/actions/workflow/status/Loriage/ReplayGain-Watcher/ci.yml?label=build)](https://github.com/Loriage/ReplayGain-Watcher/actions/workflows/ci.yml)

ReplayGain Watcher is a small, self-hosted monitor for mounted music libraries. It discovers folders containing supported audio files, waits until their imports finish, runs `rsgain easy <folder>`, verifies the generated tags, and keeps an auditable job history.

The application is deliberately monitoring-oriented. It does not edit arbitrary tags, expose audio files, open a general-purpose tag editor, or depend on `rsgain --skip-existing` as its state store.

> **Security warning:** Do not expose this web interface directly to the Internet. ReplayGain Watcher does not provide built-in user authentication. Keep port `3345` on a private network, or place the dashboard behind an authenticated reverse proxy, VPN, or equivalent access control before making it reachable from outside your trusted network.

## Quick Start

Create a `docker-compose.yml`:

```yaml
services:
  replaygain-watcher:
    image: ghcr.io/loriage/replaygain-watcher:latest
    restart: unless-stopped
    ports:
      - "3345:8080"
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Europe/Paris
    volumes:
      - ./config:/config
      - ./data/library-one:/libraries/library-one:rw
      - ./data/library-two:/libraries/library-two:rw
```

Create the host directories before starting:

```text
mkdir -p config data/library-one data/library-two
```

Set `PUID` and `PGID` to the host user and group that should own files created in the mounted directories. Replace `1000` with the values reported by `id` when necessary.

Create `config/config.yml`:

```yaml
libraries:
  - name: library-one
    path: /libraries/library-one
    enabled: true
    scan_interval_seconds: 900
    settle_seconds: 300

  - name: library-two
    path: /libraries/library-two
    enabled: true
    scan_interval_seconds: 900
    settle_seconds: 300
```

The `./config` directory is mounted at `/config` and stores both `config.yml` and the ReplayGain Watcher database. Change only the host-side paths under `./data`; keep `/libraries/library-one` and `/libraries/library-two` in `config/config.yml`.

Make sure the mounted directories are writable by the selected PUID/PGID, then start the service:

```text
docker compose up -d
```

Open `http://localhost:3345`. Readiness is reported at `/health/ready`; metrics are available at `/metrics`.

If you are running from a checkout of this repository, replace `image` with `build: .` and start with `docker compose up -d --build`.

The image is based on Debian trixie because its official repositories provide the `rsgain` package on amd64, armhf, and arm64. The published image targets `linux/amd64`, `linux/arm/v7`, and `linux/arm64`. Verification uses a read-only metadata parser and never writes tags.

The entrypoint applies the selected PUID/PGID, drops privileges before starting the application, and does not mount the Docker socket. The dashboard must remain behind your existing authenticated reverse proxy, VPN, or private network; do not publish it directly to the Internet.

## Configuration

Libraries are declared in the startup YAML file. HTTP requests cannot add a path or submit a path for processing. Environment settings can tune scan and job behavior:

| Setting | Default | Purpose |
| --- | --- | --- |
| `PUID` | `1000` | UID used to run the container process |
| `PGID` | `1000` | GID used to run the container process |
| `TZ` | `UTC` | Timezone used for dashboard dates and container log timestamps |
| `CONFIG_FILE` | `/config/config.yml` | Startup YAML file containing the declared libraries |
| `DATABASE_URL` | `sqlite+aiosqlite:////config/replaygain-watcher.db` | SQLite database location |
| `RECONCILIATION_INTERVAL_SECONDS` | `900` | Periodic source-of-truth scan |
| `SETTLE_SECONDS` | `300` | Required stable interval before queueing |
| `WORKER_CONCURRENCY` | `1` | Maximum concurrent rsgain jobs |
| `JOB_TIMEOUT_SECONDS` | `14400` | Maximum analysis duration |
| `JOB_TERMINATION_GRACE_SECONDS` | `30` | SIGTERM grace period before SIGKILL |
| `CONFIG_CHANGE_POLICY` | `mark` | Mark or automatically requeue on config changes |
| `RECOVERY_POLICY` | `requeue` | Requeue jobs interrupted by restart |
| `UI_ACTIONS_ENABLED` | `true` | Enable guarded scan/retry/requeue/cancel actions; set to `false` for read-only mode |
| `LOG_RETENTION_DAYS` | `30` | Structured job-log retention |
| `FOLLOW_SYMLINKS` | `false` | Follow symlinks during scans |
| `STAY_ON_FILESYSTEM` | `true` | Do not cross filesystem devices |

The source fingerprint uses sorted relative paths, file sizes, and nanosecond mtimes. It does not hash complete audio files during normal reconciliation. After a successful run, the fingerprint is rebuilt because ReplayGain tag writes can change file metadata; this prevents the watcher from reprocessing its own successful write.

## Processing behavior

- A periodic reconciliation is always the source of truth; filesystem events are not required.
- A new or changed folder must contain at least one supported file directly in that folder.
- Temporary suffixes such as `.part`, `.partial`, `.tmp`, `.download`, `.crdownload`, and `.!qB` postpone processing.
- A complete folder is passed to `rsgain`; individual tracks are never queued separately.
- A complete folder whose files already contain the required ReplayGain tags is marked `Skipped` and is not passed to `rsgain`.
- There is one active job per folder, claimed atomically in SQLite before starting the subprocess.
- stdout and stderr are streamed into structured `JobLog` records, with bounded tails retained on the job.
- Successful jobs are valid only after every expected file has ReplayGain track gain and, when enabled, album gain.
- Failed jobs remain visible and do not loop forever. A source change or an explicitly enabled retry can run them again.
- At startup, jobs left in `running` state are marked interrupted and requeued according to `RECOVERY_POLICY`.

## Monitoring API

Read-only routes are available under `/api/v1`:

```text
GET /status
GET /libraries
GET /libraries/{id}
POST /libraries/{id}/scan
GET /albums
GET /albums/{id}
GET /jobs
GET /jobs/{id}
GET /jobs/{id}/logs
```

Guarded actions are available from the dashboard and library pages when `UI_ACTIONS_ENABLED=true`: scan a library, retry a job, requeue a folder, or cancel a queued job. They require the CSRF cookie/header pair and are rate-limited in-process. Set `UI_ACTIONS_ENABLED=false` to keep the UI read-only. Use an authenticated reverse proxy for access control; the application intentionally does not pretend to be an identity provider.

## Navidrome

Keep ReplayGain Watcher and Navidrome as separate containers. The watcher needs read/write access to the music mounts; Navidrome can keep its own music mounts read-only. Schedule Navidrome's normal library scan after the typical settle plus processing window. ReplayGain Watcher does not use the Docker socket, restart Navidrome, or issue Navidrome API calls in the MVP.

## Development

The project has no frontend build chain. Install the package and development dependencies with Python 3.13, then run:

```text
python -m pip install -e ".[dev]"
ruff check .
pytest
```

The test suite uses temporary filesystem roots and mocked executables; it never requires a real music library. The CI workflow builds all three supported platforms with Buildx, generates an SPDX SBOM for the amd64 smoke image, and scans the image for vulnerabilities. A release build can be published with:

```text
docker buildx build \
  --platform linux/amd64,linux/arm/v7,linux/arm64 \
  --tag ghcr.io/OWNER/replaygain-watcher:latest \
  --provenance=true --sbom=true --push .
```

Buildx may use QEMU for ARM targets; Docker documents `linux/arm/v7` as the platform spelling and supports emitting one multi-platform manifest from the comma-separated platform list.

## License

ReplayGain Watcher is released under the [MIT License](LICENSE).

## Minidisc

Looking for a Navidrome client on iOS? Check out [Minidisc](https://github.com/Loriage/Minidisc), an iOS Navidrome client with ReplayGain support.
