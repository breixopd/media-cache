# Media Cache

Media Cache is a playback-aware storage tiering service for self-hosted media
stacks. It promotes requested media into a local cache, tracks usage, evicts cold
content, and manages a pool of rclone-backed storage targets. Jellyfin, Plex,
Sonarr, and Radarr integrations supply playback and library context.

## Container

Release images are published for `linux/amd64` and `linux/arm64`:

```text
ghcr.io/breixopd/media-cache:v1.1.0
```

Use a release digest in production. The Homelab Toolkit service plugin owns the
deployment configuration, storage mounts, administrative token, integrations,
metrics, health checks, and update rollout.

Core mounts:

| Path | Purpose |
| --- | --- |
| `/library` | Read-only media library view |
| `/cache` | Writable local cache tier |
| `/config/rclone` | Read-only rclone configuration |
| `/state` | Persistent cache index and service state |

`MEDIA_CACHE_TOKEN` is required for administrative endpoints. Integration URLs,
API keys, cache capacity, cold-content age, uplink bandwidth, and the rclone pool
name are provided through environment variables.

### Configuration

The service reads configuration at process start. The parent deployment must
provide `MEDIA_CACHE_TOKEN`; startup health is `503` until it is present and the
eviction scheduler is running. The supported operational variables are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CACHE_DIR` / `LIBRARY_DIR` | `/cache` / `/library` | Local VFS cache and served library roots |
| `STATE_FILE` | `/state/watch_state.json` | Durable watch and pin state |
| `THROUGHPUT_STATE_FILE` | `/state/throughput_samples.json` | Durable bandwidth samples |
| `CACHE_MAX_GB` | `500` | Capacity reported to operators |
| `COLD_AFTER_DAYS` | `15` | Age before an unpinned tracked file is evicted |
| `RCLONE_REMOTE` | `media-union` | Union remote rebuilt by backend management |
| `UPLINK_MBPS` | auto | Manual uplink override; `0` enables observation/link detection |

`SONARR_URL`, `SONARR_API_KEY`, `RADARR_URL`, `RADARR_API_KEY`, `JELLYFIN_URL`,
and `JELLYFIN_API_KEY` enable metadata lookups. Missing integration keys disable
only the corresponding lookup; they do not bypass administrative authentication.

### Integration contract

These paths and response shapes are the stable v1 contract consumed by the
Homelab Toolkit service plugin. `GET /health` is the readiness check, `GET
/api/status` is the operator status document, `GET /api/backends` lists rclone
remotes, `GET /api/active-prefetch` and `GET /api/watch-state` expose live state,
and `GET /metrics` exposes Prometheus text-format metrics. Mutating API requests
(`POST /api/pin`, `/api/unpin`, `/api/backends/add`, `/api/backends/remove`, and
`/api/backends/rebuild-pool`) require the `X-Media-Cache-Token` header.

`POST /webhook/jellyfin`, `POST /webhook/plex`, and `POST /webhook/tautulli`
retain their playback-event behavior. Jellyfin receives JSON playback events;
Plex receives its native form-encoded `payload`; Tautulli receives JSON playback
events. Webhooks remain unauthenticated because the parent deployment keeps the
service on the internal network and the existing media integrations call them
directly. The service rejects non-object JSON payloads and never returns
third-party API or rclone error text.

State files are written with atomic replacement, file flushing, directory
durability, and mode `0600`. If JSON is corrupt, it is quarantined beside the
configured state path and the service starts with an empty state so an operator
can recover the evidence without preventing health checks.

## Development

```bash
uv sync --all-extras --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=. --cov-report=term-missing --cov-fail-under=45
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements-audit.txt
uv run pip-audit --strict --requirement requirements-audit.txt
rm requirements-audit.txt
docker build -t media-cache:test .
scripts/container-smoke.sh
```

Every release tag must match the project version, builds a multi-architecture
image, publishes a digest-addressable SHA tag plus the release tag to GHCR, and
attaches a GitHub artifact attestation. There is intentionally no floating
`latest` tag.

The image runs as an unprivileged `media-cache` user with all Linux capabilities
dropped in the smoke test. Production deployments should keep `/config/rclone`
read-only, provide only the three documented storage mounts, use a digest-pinned
GHCR image, and restrict network access to the media services, operator plane,
and Prometheus scraper that need it.

## Security

Do not commit backend credentials, API keys, rclone configuration, or service
state. Report vulnerabilities using GitHub's private vulnerability reporting
flow described in [SECURITY.md](SECURITY.md).

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
