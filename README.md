# ReplayGain Watcher

ReplayGain Watcher is a small, self-hosted monitor for mounted music libraries. It discovers album directories, waits until their supported audio files stop changing, runs `rsgain easy <album-directory>`, verifies the generated tags, and keeps an auditable job history.

The application is deliberately monitoring-oriented. It does not edit arbitrary tags, expose audio files, open a general-purpose tag editor, or depend on `rsgain --skip-existing` as its state store.

## Quick Start

### Minimal setup

Create a `docker-compose.yml`:

```yaml
services:
  replaygain-watcher:
    image: ghcr.io/loriage/replaygain-watcher:latest
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./config:/config
      - ./data/library-one:/libraries/library-one:rw
```

Create the host directories before starting:

```text
mkdir -p config data/library-one
```

Create `config/config.yml`:

```yaml
libraries:
  - name: library-one
    path: /libraries/library-one
```

The `./config` directory is mounted at `/config` and stores both `config.yml` and the ReplayGain Watcher database. Change `./data/library-one` to the host path of your first music library, but keep `/libraries/library-one` in `config/config.yml`.

Make sure `./config` and the music library are writable by UID/GID `1000:1000`, then start the service:

```text
docker compose up -d
```

Open `http://localhost:8080`. Readiness is reported at `/health/ready`; metrics are available at `/metrics`.

### Complete setup

For two libraries, explicit runtime settings, automatic restarts, and the container hardening options used by the project, use:

```yaml
services:
  replaygain-watcher:
    image: ghcr.io/loriage/replaygain-watcher:latest
    container_name: replaygain-watcher
    restart: unless-stopped
    user: "1000:1000"
    ports:
      - "8080:8080"
    environment:
      TZ: Europe/Paris
      RECONCILIATION_INTERVAL_SECONDS: "900"
      SETTLE_SECONDS: "300"
      WORKER_CONCURRENCY: "1"
      UI_ACTIONS_ENABLED: "false"
      LOG_LEVEL: INFO
    volumes:
      - ./config:/config
      - ./data/library-one:/libraries/library-one:rw
      - ./data/library-two:/libraries/library-two:rw
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    tmpfs:
      - /tmp:size=256m,mode=1777
```

Create `config/config.yml` with both container paths:

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

Change only the host-side paths under `./data` when your libraries live elsewhere. If you are running from a checkout of this repository, replace `image` with `build: .` and start with `docker compose up -d --build`.

The image is based on Debian trixie because its official repositories provide the `rsgain` package on amd64, armhf, and arm64. The published image targets `linux/amd64`, `linux/arm/v7`, and `linux/arm64`. `ffprobe` is installed for operational compatibility, while verification uses a read-only metadata parser and never writes tags.

The container runs as UID/GID `1000:1000`, drops all Linux capabilities, enables `no-new-privileges`, and does not mount the Docker socket. Put the dashboard behind your existing authenticated reverse proxy before exposing it outside a trusted network.

## Configuration

Libraries are declared in the startup YAML file. HTTP requests cannot add a path or submit a path for processing. Environment settings can tune scan and job behavior:

| Setting | Default | Purpose |
| --- | --- | --- |
| `CONFIG_FILE` | `/config/config.yml` | Startup YAML file containing the declared libraries |
| `DATABASE_URL` | `sqlite+aiosqlite:////config/replaygain-watcher.db` | SQLite database location |
| `RECONCILIATION_INTERVAL_SECONDS` | `900` | Periodic source-of-truth scan |
| `SETTLE_SECONDS` | `300` | Required stable interval before queueing |
| `WORKER_CONCURRENCY` | `1` | Maximum concurrent rsgain jobs |
| `JOB_TIMEOUT_SECONDS` | `14400` | Maximum analysis duration |
| `JOB_TERMINATION_GRACE_SECONDS` | `30` | SIGTERM grace period before SIGKILL |
| `CONFIG_CHANGE_POLICY` | `mark` | Mark or automatically requeue on config changes |
| `RECOVERY_POLICY` | `requeue` | Requeue jobs interrupted by restart |
| `UI_ACTIONS_ENABLED` | `false` | Enable guarded retry/requeue/cancel actions |
| `LOG_RETENTION_DAYS` | `30` | Structured job-log retention |
| `FOLLOW_SYMLINKS` | `false` | Follow symlinks during scans |
| `STAY_ON_FILESYSTEM` | `true` | Do not cross filesystem devices |

The source fingerprint uses sorted relative paths, file sizes, and nanosecond mtimes. It does not hash complete audio files during normal reconciliation. After a successful run, the fingerprint is rebuilt because ReplayGain tag writes can change file metadata; this prevents the watcher from reprocessing its own successful write.

## Processing behavior

- A periodic reconciliation is always the source of truth; filesystem events are not required.
- A new or changed album must contain at least one supported file directly in the album directory.
- Temporary suffixes such as `.part`, `.partial`, `.tmp`, `.download`, `.crdownload`, and `.!qB` postpone processing.
- A complete album directory is passed to `rsgain`; individual tracks are never queued separately.
- There is one active job per album, claimed atomically in SQLite before starting the subprocess.
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
GET /albums
GET /albums/{id}
GET /jobs
GET /jobs/{id}
GET /jobs/{id}/logs
```

Optional actions, hidden unless `UI_ACTIONS_ENABLED=true`, are reconciliation, retry, album requeue, and queued-job cancellation. They require the CSRF cookie/header pair and are rate-limited in-process. Use an authenticated reverse proxy for access control; the application intentionally does not pretend to be an identity provider.

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
