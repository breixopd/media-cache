# Media Cache

Media Cache is a playback-aware storage tiering service for self-hosted media
stacks. It promotes requested media into a local cache, tracks usage, evicts cold
content, and manages a pool of rclone-backed storage targets. Jellyfin, Plex,
Sonarr, and Radarr integrations supply playback and library context.

## Container

Release images are published for `linux/amd64` and `linux/arm64`:

```text
ghcr.io/breixopd/media-cache:v1.0.0
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

Every release tag builds a multi-architecture image, publishes immutable SHA and
release tags to GHCR, and attaches a GitHub artifact attestation.

## Security

Do not commit backend credentials, API keys, rclone configuration, or service
state. Report vulnerabilities using GitHub's private vulnerability reporting
flow described in [SECURITY.md](SECURITY.md).

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
