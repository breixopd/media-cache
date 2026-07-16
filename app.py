"""
Media Cache Manager — smart prefetch, eviction scheduling, and cache management.

Workflow:
  1. New downloads land on local disk (handled by Sonarr/Radarr)
  2. Jellyfin/Plex send playback webhooks here
  3. On episode start: prefetch remaining season episodes (full files)
  4. On movie start: ensure full file is cached
  5. Eviction scheduler moves unwatched content to remote storage after COLD_AFTER_DAYS
  6. Watching anything resets its eviction timer
"""

import hmac
import json
import logging
import math
import os
import re
import subprocess  # nosec B404
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from time import perf_counter

import requests
from apscheduler.schedulers.background import BackgroundScheduler  # pyright: ignore[reportMissingImports]
from flask import Flask, jsonify, request

app = Flask(__name__)
log = logging.getLogger("media-cache")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
MAX_REQUEST_BYTES = 1024 * 1024
MAX_BACKEND_PARAMS = 32
MAX_BACKEND_PARAM_VALUE_LENGTH = 4096
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

# Shared-secret guard for mutating/admin endpoints. The backend-add endpoint accepts
# rclone credentials, so a missing token is a service configuration error rather than
# an authentication bypass. Webhooks, health, metrics, and read-only GETs remain open
# to media services on the internal network.
MEDIA_CACHE_TOKEN = os.getenv("MEDIA_CACHE_TOKEN", "")
_PROTECTED_ENDPOINTS = frozenset(
    {
        "/api/pin",
        "/api/unpin",
        "/api/backends/add",
        "/api/backends/remove",
        "/api/backends/rebuild-pool",
    }
)


@app.before_request
def _require_admin_token():
    if request.method != "POST" or request.path not in _PROTECTED_ENDPOINTS:
        return None
    if not MEDIA_CACHE_TOKEN:
        return jsonify({"error": "administrative API token is not configured"}), 503
    supplied = request.headers.get("X-Media-Cache-Token", "")
    if not hmac.compare_digest(supplied, MEDIA_CACHE_TOKEN):
        return jsonify({"error": "unauthorized"}), 401
    return None


@app.after_request
def _set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


# --- Config ---
def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning("%s must be an integer; using default %d", name, default)
        return default
    if value < minimum:
        log.warning("%s must be at least %d; using default %d", name, minimum, default)
        return default
    return value


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("%s must be numeric; using default %.2f", name, default)
        return default
    if not math.isfinite(value) or value < minimum:
        log.warning("%s must be finite and at least %.2f; using default %.2f", name, minimum, default)
        return default
    return value


SONARR_URL = os.getenv("SONARR_URL", "http://sonarr:8989")
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")
RADARR_URL = os.getenv("RADARR_URL", "http://radarr:7878")
RADARR_API_KEY = os.getenv("RADARR_API_KEY", "")
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "http://jellyfin:8096")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")
CACHE_DIR = os.getenv("CACHE_DIR", "/cache")
LIBRARY_DIR = os.getenv("LIBRARY_DIR", "/library")
CACHE_MAX_GB = _env_int("CACHE_MAX_GB", 500, minimum=1)
COLD_AFTER_DAYS = _env_int("COLD_AFTER_DAYS", 15, minimum=1)
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "media-union")
STATE_FILE = os.getenv("STATE_FILE", "/state/watch_state.json")
UPLINK_MBPS = _env_int("UPLINK_MBPS", 0, minimum=0)
THROUGHPUT_STATE_FILE = os.getenv("THROUGHPUT_STATE_FILE", "/state/throughput_samples.json")
DEFAULT_UPLINK_MBPS = 500
PREFETCH_SAMPLE_LIMIT = 25
AVG_EPISODE_SIZE_GB = _env_float("AVG_EPISODE_SIZE_GB", 1.5, minimum=0.01)
AVG_MOVIE_SIZE_GB = _env_float("AVG_MOVIE_SIZE_GB", 8.0, minimum=0.01)
STREAM_4K_MBPS = _env_int("STREAM_4K_MBPS", 25, minimum=1)
STREAM_1080P_MBPS = _env_int("STREAM_1080P_MBPS", 10, minimum=1)


# Paths arrive from Sonarr/Radarr (/data/tv, /data/movies), Jellyfin (/data/media/*)
# and Plex (/data/*); media-cache mounts the served library at LIBRARY_DIR (/library).
# Translate those container-namespace prefixes onto LIBRARY_DIR so prefetch/eviction
# actually act on the rclone-backed files instead of silently skipping (path not found).
def _translate_to_library(path: str) -> str:
    """Map an incoming arr/Jellyfin/Plex container path onto LIBRARY_DIR."""
    if not path:
        return ""
    norm = path.replace("\\", "/")
    library_root = Path(LIBRARY_DIR).resolve()
    if norm.startswith(LIBRARY_DIR.rstrip("/") + "/") or norm == LIBRARY_DIR:
        candidate = Path(norm)
    else:
        candidate = None
        for prefix in ("/data/media/", "/data/tv/", "/data/movies/", "/data/music/", "/data/"):
            if not norm.startswith(prefix):
                continue
            relative = norm[len(prefix) :]
            source_group = prefix.strip("/").split("/", 1)
            if len(source_group) == 2 and source_group[1] in {"tv", "movies", "music"}:
                relative = f"{source_group[1]}/{relative}"
            candidate = library_root / relative
            break
        if candidate is None:
            return ""
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(library_root)
    except (OSError, ValueError):
        return ""
    return str(resolved)


_lock = threading.Lock()
_watch_state: dict[str, dict] = {}
_throughput_samples: list[dict[str, float | str]] = []
_prefetch_active: set[str] = set()
_prefetch_pool = ThreadPoolExecutor(max_workers=4)
_prefetch_lock = threading.Lock()
_metrics_lock = threading.Lock()

_metrics_webhooks_total = 0
_metrics_prefetch_started = 0
_metrics_prefetch_completed = 0
_metrics_evictions_total = 0


def _claim_prefetch(path: str) -> bool:
    global _metrics_prefetch_started
    with _prefetch_lock:
        if path in _prefetch_active:
            return False
        _prefetch_active.add(path)
    with _metrics_lock:
        _metrics_prefetch_started += 1
    return True


def _release_prefetch(path: str) -> None:
    with _prefetch_lock:
        _prefetch_active.discard(path)


def _load_state() -> None:
    global _watch_state
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("watch state must be an object")
        _watch_state = {path: state for path, state in raw.items() if isinstance(path, str) and isinstance(state, dict)}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        _watch_state = {}


def _save_state() -> None:
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_watch_state, f)
    os.replace(tmp, STATE_FILE)


def _load_throughput_samples() -> None:
    global _throughput_samples
    try:
        with open(THROUGHPUT_STATE_FILE) as f:
            raw = json.load(f)
        if isinstance(raw, list):
            samples = [
                sample
                for sample in raw
                if isinstance(sample, dict)
                and isinstance(sample.get("mbps"), int | float)
                and isinstance(sample.get("type"), str)
                and isinstance(sample.get("timestamp"), str)
            ]
            _throughput_samples = samples[-PREFETCH_SAMPLE_LIMIT:]
        else:
            _throughput_samples = []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _throughput_samples = []


def _save_throughput_samples() -> None:
    Path(THROUGHPUT_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    tmp = THROUGHPUT_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_throughput_samples[-PREFETCH_SAMPLE_LIMIT:], f)
    os.replace(tmp, THROUGHPUT_STATE_FILE)


def _record_throughput(bytes_read: int, elapsed_seconds: float, sample_type: str) -> None:
    if bytes_read <= 0 or elapsed_seconds <= 0:
        return

    mbps = (bytes_read * 8) / (elapsed_seconds * 1_000_000)
    sample = {
        "timestamp": datetime.now(UTC).isoformat(),
        "type": sample_type,
        "mbps": round(mbps, 2),
    }

    with _lock:
        _throughput_samples.append(sample)
        del _throughput_samples[:-PREFETCH_SAMPLE_LIMIT]
        _save_throughput_samples()


def _median_throughput(sample_types: tuple[str, ...] | None = None) -> float | None:
    with _lock:
        samples = list(_throughput_samples)

    if sample_types is not None:
        samples = [sample for sample in samples if sample.get("type") in sample_types]

    values = [float(sample.get("mbps", 0.0)) for sample in samples if float(sample.get("mbps", 0.0)) > 0]
    if not values:
        return None
    return float(median(values))


def _detect_link_speed_mbps() -> int | None:
    net_root = Path("/sys/class/net")
    if not net_root.exists():
        return None

    for iface in net_root.iterdir():
        if iface.name == "lo":
            continue
        try:
            operstate = (iface / "operstate").read_text().strip()
            speed = int((iface / "speed").read_text().strip())
        except (FileNotFoundError, OSError, ValueError):
            continue
        if operstate in {"up", "unknown"} and speed > 0:
            return speed

    return None


def _effective_bandwidth_profile() -> dict[str, float | int | str]:
    configured = UPLINK_MBPS if UPLINK_MBPS > 0 else None
    observed = _median_throughput()
    observed_head = _median_throughput(("head",))
    link_speed = _detect_link_speed_mbps()

    if configured:
        effective = float(configured)
        source = "configured"
    elif observed:
        effective = observed
        source = "observed"
    elif link_speed:
        effective = float(link_speed)
        source = "link"
    else:
        effective = float(DEFAULT_UPLINK_MBPS)
        source = "default"

    head_effective = observed_head or observed or effective
    return {
        "configured_uplink_mbps": configured or 0,
        "effective_uplink_mbps": round(effective, 1),
        "head_uplink_mbps": round(float(head_effective), 1),
        "observed_uplink_mbps": round(observed, 1) if observed else 0.0,
        "source": source,
        "observed_samples": len(_throughput_samples),
    }


def _touch_watched(file_path):
    """Mark a file as recently watched — resets eviction timer."""
    file_path = _translate_to_library(str(file_path))
    if not file_path:
        return False
    with _lock:
        _watch_state[file_path] = {
            "last_watched": datetime.now(UTC).isoformat(),
            "pinned": _watch_state.get(file_path, {}).get("pinned", False),
        }
        _save_state()
    return True


# --- API helpers ---


def _sonarr_get(path):
    if not SONARR_API_KEY:
        return None
    try:
        r = requests.get(
            f"{SONARR_URL}/api/v3{path}",
            headers={"X-Api-Key": SONARR_API_KEY},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("sonarr: %s", e)
        return None


def _radarr_get(path):
    if not RADARR_API_KEY:
        return None
    try:
        r = requests.get(
            f"{RADARR_URL}/api/v3{path}",
            headers={"X-Api-Key": RADARR_API_KEY},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("radarr: %s", e)
        return None


def _get_season_episodes(series_id, season):
    """Get all episodes in a season from Sonarr."""
    episodes = _sonarr_get(f"/episode?seriesId={series_id}")
    if not episodes:
        return []
    return [
        ep
        for ep in sorted(
            episodes,
            key=lambda e: (e.get("seasonNumber", 0), e.get("episodeNumber", 0)),
        )
        if ep.get("seasonNumber") == season and ep.get("hasFile") and ep.get("episodeFile", {}).get("path")
    ]


def _get_remaining_season_paths(series_id, season, from_episode):
    """Get file paths for all episodes from from_episode onward in the season."""
    season_eps = _get_season_episodes(series_id, season)
    return [ep["episodeFile"]["path"] for ep in season_eps if ep.get("episodeNumber", 0) >= from_episode]


HEAD_BYTES = 100 * 1024 * 1024  # 100MB — enough for media server to start buffering


def _prefetch_full_file(path, claimed=False):
    """Read the entire file to pull it into rclone VFS cache."""
    global _metrics_prefetch_completed
    path = _translate_to_library(path)
    if not path:
        return
    if not os.path.exists(path):
        log.info("prefetch skip (not found): %s", path)
        if claimed:
            _release_prefetch(path)
        return
    if not claimed and not _claim_prefetch(path):
        return
    started = perf_counter()
    try:
        size = os.path.getsize(path)
        log.info("prefetching %s (%.1f GB)...", Path(path).name, size / 1073741824)
        chunk = 8 * 1024 * 1024  # 8MB chunks
        with open(path, "rb") as f:
            while f.read(chunk):
                pass
        _record_throughput(size, perf_counter() - started, "full")
        _touch_watched(path)
        with _metrics_lock:
            _metrics_prefetch_completed += 1
        log.info("prefetched: %s", Path(path).name)
    except Exception as e:
        log.warning("prefetch failed: %s — %s", path, e)
    finally:
        _release_prefetch(path)


def _prefetch_priority_file(path):
    """Fast-start prefetch: read first 100MB immediately, then background the rest.

    At 500Mbps this takes ~1.6 seconds for the head, and the media server
    can start playing while the remainder downloads.
    """
    path = _translate_to_library(path)
    if not path:
        return False
    if not os.path.exists(path):
        log.info("priority prefetch skip: %s", path)
        return False
    if not _claim_prefetch(path):
        return False
    started = perf_counter()
    try:
        size = os.path.getsize(path)
        log.info(
            "priority prefetch %s (%.1f GB) — reading head...",
            Path(path).name,
            size / 1073741824,
        )
        chunk = 16 * 1024 * 1024  # 16MB chunks for priority reads
        with open(path, "rb") as f:
            read = 0
            while read < HEAD_BYTES:
                data = f.read(chunk)
                if not data:
                    break
                read += len(data)
        _record_throughput(read, perf_counter() - started, "head")
        log.info(
            "head cached (%d MB), continuing in background: %s",
            read // (1024 * 1024),
            Path(path).name,
        )
        _touch_watched(path)
        _prefetch_pool.submit(_prefetch_tail, path, read)
        return True
    except Exception as e:
        log.warning("priority prefetch failed: %s — %s", path, e)
        _release_prefetch(path)
        return False


def _prefetch_tail(path, offset):
    """Read remainder of file from offset (background continuation)."""
    started = perf_counter()
    try:
        bytes_read = 0
        chunk = 8 * 1024 * 1024
        with open(path, "rb") as f:
            f.seek(offset)
            while True:
                data = f.read(chunk)
                if not data:
                    break
                bytes_read += len(data)
        _record_throughput(bytes_read, perf_counter() - started, "tail")
        log.info("prefetch complete: %s", Path(path).name)
    except Exception as e:
        log.warning("tail prefetch failed: %s — %s", path, e)
    finally:
        _release_prefetch(path)


def _prefetch_paths_background(paths):
    """Prefetch a list of paths in background (bounded concurrency)."""
    for p in paths:
        path = _translate_to_library(p)
        if not path or not _claim_prefetch(path):
            continue
        _touch_watched(path)
        try:
            _prefetch_pool.submit(_prefetch_full_file, path, True)
        except RuntimeError:
            _release_prefetch(path)
            log.exception("prefetch queue rejected %s", path)


def _resolve_sonarr_series(title):
    """Find a Sonarr series by title (case-insensitive)."""
    series_list = _sonarr_get("/series")
    if not series_list:
        return None
    title_lower = title.lower()
    for s in series_list:
        if s.get("title", "").lower() == title_lower:
            return s
    return None


# --- Eviction scheduler ---


def _eviction_check():
    """Move unwatched content to remote storage."""
    cutoff = datetime.now(UTC) - timedelta(days=COLD_AFTER_DAYS)
    to_evict = []
    invalid_paths = []
    with _lock:
        for path, state in list(_watch_state.items()):
            if not isinstance(state, dict):
                invalid_paths.append(path)
                continue
            if state.get("pinned"):
                continue
            try:
                last = datetime.fromisoformat(str(state.get("last_watched", "2000-01-01")))
            except ValueError:
                invalid_paths.append(path)
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if last < cutoff:
                to_evict.append(path)
        for path in invalid_paths:
            _watch_state.pop(path, None)

    global _metrics_evictions_total
    for path in to_evict:
        safe_path = _translate_to_library(path)
        if not safe_path:
            with _lock:
                _watch_state.pop(path, None)
            continue
        path = safe_path
        if not os.path.exists(path):
            with _lock:
                _watch_state.pop(path, None)
            continue
        log.info("evicting (unwatched %dd): %s", COLD_AFTER_DAYS, Path(path).name)
        with _metrics_lock:
            _metrics_evictions_total += 1
        # rclone VFS handles this — clearing the local cache entry triggers
        # the file to only exist on remote. We just need to drop it from VFS cache.
        try:
            cache_path = Path(CACHE_DIR)
            # Remove the cached copy using the full relative path
            rel = os.path.relpath(path, LIBRARY_DIR)
            candidate = cache_path / rel
            if candidate.is_file():
                candidate.unlink()
                log.info("evicted from cache: %s", candidate)
        except Exception as e:
            log.warning("eviction failed: %s — %s", path, e)

        with _lock:
            _watch_state.pop(path, None)
    with _lock:
        if to_evict or invalid_paths:
            _save_state()


# --- Webhook endpoints ---


@app.route("/webhook/jellyfin", methods=["POST"])
def jellyfin_webhook():
    """Handle Jellyfin playback webhook — prefetch rest of season or movie."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be an object"}), 400
    event = data.get("NotificationType", "")
    if event not in ("PlaybackStart",):
        return jsonify({"status": "ignored", "event": event})

    global _metrics_webhooks_total
    with _metrics_lock:
        _metrics_webhooks_total += 1

    item_id = data.get("ItemId", "")
    if not item_id:
        return jsonify({"status": "no item"})

    if not JELLYFIN_API_KEY:
        return jsonify({"status": "no jellyfin api key"})

    try:
        r = requests.get(
            f"{JELLYFIN_URL}/Items/{item_id}",
            params={"api_key": JELLYFIN_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        item = r.json()
    except Exception as e:
        log.warning("jellyfin item lookup: %s", e)
        return jsonify({"status": "lookup failed"})

    item_type = item.get("Type", "")

    # Episode — priority-prefetch current, background the rest
    if item_type == "Episode":
        series_name = item.get("SeriesName", "")
        season = item.get("ParentIndexNumber", 0)
        episode = item.get("IndexNumber", 0)
        series = _resolve_sonarr_series(series_name)
        if not series:
            return jsonify({"status": "series not in sonarr"})
        paths = _get_remaining_season_paths(series["id"], season, episode)
        if paths:
            _prefetch_priority_file(paths[0])
            _prefetch_paths_background(paths[1:])
        return jsonify({"status": "prefetching season", "files": len(paths)})

    # Movie — priority-prefetch (fast-start head, then background rest)
    if item_type == "Movie":
        path = item.get("Path", "")
        if path:
            _touch_watched(path)
            _prefetch_priority_file(path)
            return jsonify({"status": "prefetching movie"})
        # Try Radarr lookup
        title = item.get("Name", "")
        movies = _radarr_get("/movie")
        if movies:
            for m in movies:
                if m.get("title", "").lower() == title.lower() and m.get("hasFile"):
                    mpath = m.get("movieFile", {}).get("path", "")
                    if mpath:
                        _touch_watched(mpath)
                        _prefetch_priority_file(mpath)
                        return jsonify({"status": "prefetching movie via radarr"})
        return jsonify({"status": "movie path not found"})

    return jsonify({"status": "unsupported type", "type": item_type})


@app.route("/webhook/plex", methods=["POST"])
def plex_webhook():
    """Handle native Plex playback webhook."""
    payload = request.form.get("payload")
    if not payload:
        return jsonify({"status": "no payload"})
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return jsonify({"status": "bad json"})
    if not isinstance(data, dict):
        return jsonify({"status": "bad json"}), 400

    event = data.get("event", "")
    if event not in ("media.play", "media.resume"):
        return jsonify({"status": "ignored"})

    global _metrics_webhooks_total
    with _metrics_lock:
        _metrics_webhooks_total += 1

    metadata = data.get("Metadata", {})
    if not isinstance(metadata, dict):
        return jsonify({"error": "invalid metadata"}), 400
    media_type = metadata.get("type", "")

    # Episode — priority-prefetch current, background the rest
    if media_type == "episode":
        series_title = metadata.get("grandparentTitle", "")
        try:
            season = int(metadata.get("parentIndex", 0))
            episode_num = int(metadata.get("index", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid episode metadata"}), 400
        series = _resolve_sonarr_series(series_title)
        if not series:
            return jsonify({"status": "series not in sonarr"})
        paths = _get_remaining_season_paths(series["id"], season, episode_num)
        if paths:
            _prefetch_priority_file(paths[0])
            _prefetch_paths_background(paths[1:])
        return jsonify({"status": "prefetching season", "files": len(paths)})

    # Movie — priority-prefetch
    if media_type == "movie":
        title = metadata.get("title", "")
        movies = _radarr_get("/movie")
        if movies:
            for m in movies:
                if m.get("title", "").lower() == title.lower() and m.get("hasFile"):
                    path = m.get("movieFile", {}).get("path", "")
                    if path:
                        _touch_watched(path)
                        _prefetch_priority_file(path)
                        return jsonify({"status": "prefetching movie"})
        return jsonify({"status": "movie not found in radarr"})

    return jsonify({"status": "unsupported", "type": media_type})


@app.route("/webhook/tautulli", methods=["POST"])
def tautulli_webhook():
    """Handle Tautulli playback webhook — same logic as Plex webhook."""
    data = request.get_json(silent=True) or {}
    event = data.get("event", "")
    if event not in ("play", "resume", "watched"):
        return jsonify({"status": "ignored", "event": event})

    global _metrics_webhooks_total
    with _metrics_lock:
        _metrics_webhooks_total += 1

    metadata = data.get("Metadata", {})
    if not isinstance(metadata, dict):
        return jsonify({"error": "invalid metadata"}), 400
    media_type = metadata.get("type", "")

    if media_type == "episode":
        series_title = metadata.get("grandparentTitle", "")
        try:
            season = int(metadata.get("parentIndex", 0))
            episode_num = int(metadata.get("index", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid episode metadata"}), 400
        series = _resolve_sonarr_series(series_title)
        if not series:
            return jsonify({"status": "series not in sonarr"})
        paths = _get_remaining_season_paths(series["id"], season, episode_num)
        if paths:
            _prefetch_priority_file(paths[0])
            _prefetch_paths_background(paths[1:])
        return jsonify({"status": "prefetching season", "files": len(paths)})

    if media_type == "movie":
        title = metadata.get("title", "")
        movies = _radarr_get("/movie")
        if movies:
            for m in movies:
                if m.get("title", "").lower() == title.lower() and m.get("hasFile"):
                    path = m.get("movieFile", {}).get("path", "")
                    if path:
                        _touch_watched(path)
                        _prefetch_priority_file(path)
                        return jsonify({"status": "prefetching movie"})
        return jsonify({"status": "movie not found in radarr"})

    return jsonify({"status": "unsupported", "type": media_type})


def _normalize_library_path(path: str) -> str | None:
    """Resolve path under LIBRARY_DIR for pin/unpin."""
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(LIBRARY_DIR) / path
    try:
        resolved = candidate.resolve()
        lib_root = Path(LIBRARY_DIR).resolve()
        if lib_root in resolved.parents or resolved == lib_root:
            return str(resolved)
    except OSError:
        return None
    return None


@app.route("/api/pin", methods=["POST"])
def pin_file():
    """Pin a library file (e.g. while Tdarr transcodes) to prevent eviction."""
    data = request.get_json(silent=True) or {}
    path = _normalize_library_path(str(data.get("path", "")))
    if not path:
        return jsonify({"status": "error", "detail": "invalid path"}), 400
    with _lock:
        state = _watch_state.get(path, {})
        state["pinned"] = True
        state["last_watched"] = datetime.now(UTC).isoformat()
        _watch_state[path] = state
        _save_state()
    log.info("pinned: %s", path)
    return jsonify({"status": "pinned", "path": path})


@app.route("/api/unpin", methods=["POST"])
def unpin_file():
    """Clear transcode pin; eviction may proceed per cold_after_days."""
    data = request.get_json(silent=True) or {}
    path = _normalize_library_path(str(data.get("path", "")))
    if not path:
        return jsonify({"status": "error", "detail": "invalid path"}), 400
    with _lock:
        state = _watch_state.get(path, {})
        state["pinned"] = False
        _watch_state[path] = state
        _save_state()
    log.info("unpinned: %s", path)
    return jsonify({"status": "unpinned", "path": path})


# --- Management API ---


@app.route("/api/status")
def cache_status():
    cache_path = Path(CACHE_DIR)
    try:
        result = subprocess.run(  # nosec B603 B607
            ["du", "-sb", str(cache_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        cache_bytes = int(result.stdout.split()[0]) if result.returncode == 0 else 0
    except Exception:
        cache_bytes = 0

    bandwidth = _effective_bandwidth_profile()
    effective_uplink = float(bandwidth["effective_uplink_mbps"])
    head_uplink = float(bandwidth["head_uplink_mbps"])

    episode_fetch_secs = (AVG_EPISODE_SIZE_GB * 1073741824 * 8) / max(effective_uplink * 1_000_000, 1)
    movie_fetch_secs = (AVG_MOVIE_SIZE_GB * 1073741824 * 8) / max(effective_uplink * 1_000_000, 1)
    first_frame_secs = (HEAD_BYTES * 8) / max(head_uplink * 1_000_000, 1)

    max_4k_streams = int(effective_uplink // STREAM_4K_MBPS)
    max_1080p_streams = int(effective_uplink // STREAM_1080P_MBPS)

    with _prefetch_lock:
        active_prefetch_count = len(_prefetch_active)

    return jsonify(
        {
            "cache_used_bytes": cache_bytes,
            "cache_max_gb": CACHE_MAX_GB,
            "cache_used_pct": round(cache_bytes / (CACHE_MAX_GB * 1073741824) * 100, 1) if CACHE_MAX_GB else 0,
            "active_prefetch": active_prefetch_count,
            "tracked_files": len(_watch_state),
            "cold_after_days": COLD_AFTER_DAYS,
            "bandwidth": {
                **bandwidth,
                "episode_fetch_seconds": round(episode_fetch_secs),
                "movie_fetch_seconds": round(movie_fetch_secs),
                "time_to_first_frame_seconds": round(first_frame_secs, 1),
                "max_concurrent_4k": max_4k_streams,
                "max_concurrent_1080p": max_1080p_streams,
            },
        }
    )


@app.route("/api/backends")
def list_backends():
    try:
        result = subprocess.run(  # nosec B603 B607
            ["rclone", "listremotes"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        remotes = [r.rstrip(":") for r in result.stdout.strip().split("\n") if r.strip()]
    except Exception:
        remotes = []
    return jsonify({"backends": remotes})


def _update_union_remote():
    """Rebuild the media-union remote to include all non-union backends."""
    try:
        result = subprocess.run(  # nosec B603 B607
            ["rclone", "listremotes"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            log.warning("union rebuild: failed to list remotes")
            return False
        all_remotes = [r.rstrip(":") for r in result.stdout.strip().split("\n") if r.strip()]
        # Exclude the union remote itself and any empty names
        upstreams = [r for r in all_remotes if r and r != RCLONE_REMOTE]
        if not upstreams:
            log.info("union rebuild: no backends to join")
            return True
        # Format: "remote1: remote2: remote3:" (space-separated, colon-suffixed)
        upstreams_str = " ".join(f"{r}:" for r in upstreams)
        # Create/update the union remote
        cmd = [
            "rclone",
            "config",
            "create",
            RCLONE_REMOTE,
            "union",
            "upstreams",
            upstreams_str,
            "action_policy",
            "all",
            "create_policy",
            "all",
            "search_policy",
            "all",
        ]
        rebuild = subprocess.run(  # nosec B603 B607
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if rebuild.returncode == 0:
            log.info("union rebuild: %s now unions %s", RCLONE_REMOTE, upstreams)
            return True
        log.warning("union rebuild failed: %s", rebuild.stderr.strip())
        return False
    except Exception as e:
        log.warning("union rebuild error: %s", e)
        return False


@app.route("/api/backends/add", methods=["POST"])
def add_backend():
    """Add an rclone remote via API (used by management UI)."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be an object"}), 400
    remote_name = data.get("name", "")
    remote_type = data.get("type", "")
    params = data.get("params", {})
    if not isinstance(remote_name, str) or not isinstance(remote_type, str):
        return jsonify({"error": "name and type must be strings"}), 400
    remote_name = remote_name.strip()
    remote_type = remote_type.strip()
    if not remote_name or not remote_type:
        return jsonify({"error": "name and type required"}), 400
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$", remote_name):
        return jsonify({"error": "invalid remote name"}), 400
    if not re.match(r"^[a-z0-9]{1,32}$", remote_type):
        return jsonify({"error": "invalid remote type"}), 400
    if not isinstance(params, dict):
        return jsonify({"error": "params must be an object"}), 400
    if len(params) > MAX_BACKEND_PARAMS:
        return jsonify({"error": f"at most {MAX_BACKEND_PARAMS} params are allowed"}), 400

    # Validate param keys — prevent rclone flag injection
    for k, value in params.items():
        if not isinstance(k, str):
            return jsonify({"error": "param keys must be strings"}), 400
        if not re.match(r"^[a-z][a-z0-9_]*$", k):
            return jsonify({"error": f"invalid param key: {k}"}), 400
        if not isinstance(value, str | int | float | bool):
            return jsonify({"error": f"invalid value type for param: {k}"}), 400
        val = str(value)
        if len(val) > MAX_BACKEND_PARAM_VALUE_LENGTH:
            return jsonify({"error": f"param value is too long: {k}"}), 400
        if val.startswith("-"):
            return jsonify({"error": "param values must not start with -"}), 400
        if any(character in val for character in ("\0", "\r", "\n")):
            return jsonify({"error": f"param value contains control characters: {k}"}), 400

    cmd = ["rclone", "config", "create", remote_name, remote_type]
    for k, v in params.items():
        cmd.extend([k, str(v)])
    try:
        result = subprocess.run(  # nosec B603 B607
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("rclone rejected backend configuration for %s", remote_name)
            return jsonify({"error": "rclone rejected the backend configuration"}), 502
        # Auto-join to union pool
        pool_ok = _update_union_remote()
        return jsonify({"status": "created", "remote": remote_name, "pool_updated": pool_ok})
    except (OSError, subprocess.SubprocessError):
        log.exception("rclone backend creation failed for %s", remote_name)
        return jsonify({"error": "rclone backend creation failed"}), 502


@app.route("/api/backends/rebuild-pool", methods=["POST"])
def rebuild_pool():
    """Manually rebuild the union remote from all configured backends."""
    ok = _update_union_remote()
    if ok:
        return jsonify({"status": "rebuilt"})
    return jsonify({"error": "rebuild failed"}), 500


@app.route("/api/backends/remove", methods=["POST"])
def remove_backend():
    """Remove an rclone remote and rebuild the union pool."""
    data = request.get_json(silent=True) or {}
    remote_name = data.get("name", "").strip()
    if not remote_name:
        return jsonify({"error": "name required"}), 400
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$", remote_name):
        return jsonify({"error": "invalid remote name"}), 400
    if remote_name == RCLONE_REMOTE:
        return jsonify({"error": "cannot remove the union remote itself"}), 400
    try:
        result = subprocess.run(  # nosec B603 B607
            ["rclone", "config", "delete", remote_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            log.warning("rclone rejected backend removal for %s", remote_name)
            return jsonify({"error": "rclone rejected the backend removal"}), 502
        _update_union_remote()
        return jsonify({"status": "removed", "remote": remote_name})
    except (OSError, subprocess.SubprocessError):
        log.exception("rclone backend removal failed for %s", remote_name)
        return jsonify({"error": "rclone backend removal failed"}), 502


@app.route("/api/active-prefetch")
def active_prefetch():
    with _prefetch_lock:
        active = sorted(_prefetch_active)
    return jsonify({"active": active})


@app.route("/api/watch-state")
def watch_state_api():
    with _lock:
        return jsonify({"files": len(_watch_state), "state": _watch_state})


@app.route("/health")
def health():
    if not MEDIA_CACHE_TOKEN:
        return jsonify({"status": "misconfigured", "detail": "administrative API token is missing"}), 503
    if _scheduler is None or not _scheduler.running:
        return jsonify({"status": "unavailable", "detail": "eviction scheduler is not running"}), 503
    return jsonify({"status": "ok"})


@app.route("/metrics")
def prometheus_metrics():
    """Prometheus metrics endpoint for Grafana dashboard."""
    cache_path = Path(CACHE_DIR)
    try:
        result = subprocess.run(  # nosec B603 B607
            ["du", "-sb", str(cache_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        cache_bytes = int(result.stdout.split()[0]) if result.returncode == 0 else 0
    except Exception:
        cache_bytes = 0

    cache_max_bytes = CACHE_MAX_GB * 1073741824
    bandwidth = _effective_bandwidth_profile()
    with _lock:
        tracked = len(_watch_state)
        pinned = sum(1 for s in _watch_state.values() if s.get("pinned"))
    with _prefetch_lock:
        active = len(_prefetch_active)
    with _metrics_lock:
        webhook_count = _metrics_webhooks_total
        prefetch_started = _metrics_prefetch_started
        prefetch_completed = _metrics_prefetch_completed
        eviction_count = _metrics_evictions_total
    time_to_first_frame = (HEAD_BYTES * 8) / max(float(bandwidth["head_uplink_mbps"]) * 1_000_000, 1)

    lines = [
        "# HELP media_cache_bytes_used Current cache usage in bytes",
        "# TYPE media_cache_bytes_used gauge",
        f"media_cache_bytes_used {cache_bytes}",
        "# HELP media_cache_bytes_max Maximum cache size in bytes",
        "# TYPE media_cache_bytes_max gauge",
        f"media_cache_bytes_max {cache_max_bytes}",
        "# HELP media_cache_usage_ratio Cache usage as a ratio 0-1",
        "# TYPE media_cache_usage_ratio gauge",
        f"media_cache_usage_ratio {cache_bytes / cache_max_bytes if cache_max_bytes else 0:.4f}",
        "# HELP media_cache_tracked_files Number of files tracked in watch state",
        "# TYPE media_cache_tracked_files gauge",
        f"media_cache_tracked_files {tracked}",
        "# HELP media_cache_pinned_files Number of pinned (non-evictable) files",
        "# TYPE media_cache_pinned_files gauge",
        f"media_cache_pinned_files {pinned}",
        "# HELP media_cache_active_prefetch Number of files currently being prefetched",
        "# TYPE media_cache_active_prefetch gauge",
        f"media_cache_active_prefetch {active}",
        "# HELP media_cache_uplink_mbps Effective uplink speed used for fetch estimates",
        "# TYPE media_cache_uplink_mbps gauge",
        f"media_cache_uplink_mbps {bandwidth['effective_uplink_mbps']}",
        "# HELP media_cache_configured_uplink_mbps Manually configured uplink speed (0 means auto)",
        "# TYPE media_cache_configured_uplink_mbps gauge",
        f"media_cache_configured_uplink_mbps {bandwidth['configured_uplink_mbps']}",
        "# HELP media_cache_observed_uplink_mbps Median observed prefetch throughput",
        "# TYPE media_cache_observed_uplink_mbps gauge",
        f"media_cache_observed_uplink_mbps {bandwidth['observed_uplink_mbps']}",
        "# HELP media_cache_time_to_first_frame_seconds Estimated time to cache the first playback chunk",
        "# TYPE media_cache_time_to_first_frame_seconds gauge",
        f"media_cache_time_to_first_frame_seconds {time_to_first_frame:.2f}",
        "# HELP media_cache_cold_after_days Days before eviction",
        "# TYPE media_cache_cold_after_days gauge",
        f"media_cache_cold_after_days {COLD_AFTER_DAYS}",
        "# HELP media_cache_webhooks_total Total webhook events processed",
        "# TYPE media_cache_webhooks_total counter",
        f"media_cache_webhooks_total {webhook_count}",
        "# HELP media_cache_prefetch_started_total Total prefetch operations started",
        "# TYPE media_cache_prefetch_started_total counter",
        f"media_cache_prefetch_started_total {prefetch_started}",
        "# HELP media_cache_prefetch_completed_total Total prefetch operations completed",
        "# TYPE media_cache_prefetch_completed_total counter",
        f"media_cache_prefetch_completed_total {prefetch_completed}",
        "# HELP media_cache_evictions_total Total files evicted to remote storage",
        "# TYPE media_cache_evictions_total counter",
        f"media_cache_evictions_total {eviction_count}",
    ]
    return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; charset=utf-8"}


def _start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _eviction_check,
        "interval",
        hours=6,
        id="eviction",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.start()
    return scheduler


_scheduler = None


def _init() -> None:
    """Load persisted state and start background jobs.

    Runs at import time so it works under gunicorn (production) as well as the
    Flask dev server. Guarded so it only initializes once per process.
    """
    if getattr(_init, "_done", False):
        return
    _init._done = True  # type: ignore[attr-defined]
    _load_state()
    _load_throughput_samples()
    global _scheduler
    _scheduler = _start_scheduler()


_init()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8686)  # nosec B104
