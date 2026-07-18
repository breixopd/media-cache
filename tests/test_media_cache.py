"""Tests for the media-cache predictive caching service."""

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Stub apscheduler before importing the app module
apscheduler_stub = types.ModuleType("apscheduler")
apscheduler_sched = types.ModuleType("apscheduler.schedulers")
apscheduler_bg = types.ModuleType("apscheduler.schedulers.background")
apscheduler_bg.BackgroundScheduler = MagicMock  # type: ignore[attr-defined]
apscheduler_sched.background = apscheduler_bg  # type: ignore[attr-defined]
apscheduler_stub.schedulers = apscheduler_sched  # type: ignore[attr-defined]
sys.modules["apscheduler"] = apscheduler_stub
sys.modules["apscheduler.schedulers"] = apscheduler_sched
sys.modules["apscheduler.schedulers.background"] = apscheduler_bg

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = REPO_ROOT / "app.py"
APP_SPEC = importlib.util.spec_from_file_location("homelab_media_cache_app", APP_PATH)
assert APP_SPEC is not None and APP_SPEC.loader is not None
cache_app = importlib.util.module_from_spec(APP_SPEC)
sys.modules[APP_SPEC.name] = cache_app
APP_SPEC.loader.exec_module(cache_app)


@pytest.fixture
def client():
    cache_app.app.config["TESTING"] = True
    with cache_app.app.test_client() as c:
        yield c


class TestHealth:
    def test_health_ok(self, client):
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", "configured"):
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json["status"] == "ok"

    def test_health_fails_closed_without_admin_token(self, client):
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", ""):
            resp = client.get("/health")

        assert resp.status_code == 503
        assert resp.json["status"] == "misconfigured"

    def test_responses_set_security_headers(self, client):
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", "configured"):
            resp = client.get("/health")

        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "no-referrer"

    def test_oversized_payload_is_rejected(self, client):
        resp = client.post(
            "/webhook/jellyfin",
            data=b"x" * (cache_app.MAX_REQUEST_BYTES + 1),
            content_type="application/json",
        )

        assert resp.status_code == 413


class TestCacheStatus:
    def test_invalid_numeric_environment_uses_safe_defaults(self):
        with patch.dict(cache_app.os.environ, {"TEST_VALUE": "invalid"}):
            assert cache_app._env_int("TEST_VALUE", 42, minimum=1) == 42
            assert cache_app._env_float("TEST_VALUE", 1.5, minimum=0.1) == 1.5

    def test_out_of_range_numeric_environment_uses_safe_defaults(self):
        with patch.dict(cache_app.os.environ, {"TEST_VALUE": "0"}):
            assert cache_app._env_int("TEST_VALUE", 42, minimum=1) == 42
            assert cache_app._env_float("TEST_VALUE", 1.5, minimum=0.1) == 1.5

    @patch.object(cache_app, "_detect_link_speed_mbps", return_value=None)
    def test_status_returns_metrics(self, mock_link, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json
        assert "cache_used_bytes" in data
        assert "cache_max_gb" in data
        assert "bandwidth" in data
        assert data["bandwidth"]["effective_uplink_mbps"] == 500


class TestLibraryPaths:
    def test_translates_service_path_inside_library(self):
        with patch.object(cache_app, "LIBRARY_DIR", "/library"):
            assert cache_app._translate_to_library("/data/movies/film.mkv") == "/library/movies/film.mkv"

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/data/movies/film.mkv", "/library/movies/film.mkv"),
            ("/data/tv/show/episode.mkv", "/library/tv/show/episode.mkv"),
        ],
    )
    def test_normalizes_arr_paths_for_pin_operations(self, path, expected):
        with patch.object(cache_app, "LIBRARY_DIR", "/library"):
            assert cache_app._normalize_library_path(path) == expected

    @pytest.mark.parametrize(
        "path",
        [
            "/data/movies/../../etc/passwd",
            "/library/../state/watch_state.json",
            "/unrecognised/path.mkv",
        ],
    )
    def test_rejects_paths_outside_library(self, path):
        with patch.object(cache_app, "LIBRARY_DIR", "/library"):
            assert cache_app._translate_to_library(path) == ""

    @patch.object(cache_app, "_detect_link_speed_mbps", return_value=None)
    def test_bandwidth_calculations(self, mock_link, client):
        resp = client.get("/api/status")
        bw = resp.json["bandwidth"]
        assert bw["max_concurrent_4k"] == 20  # 500 // 25
        assert bw["max_concurrent_1080p"] == 50  # 500 // 10


class TestJellyfinWebhook:
    def test_rejects_non_object_payload(self, client):
        resp = client.post("/webhook/jellyfin", json=["not", "an", "object"])

        assert resp.status_code == 400
        assert resp.json == {"error": "request body must be an object"}

    def test_rejects_empty_json_body(self, client):
        resp = client.post("/webhook/jellyfin", data="not-json", content_type="application/json")

        assert resp.status_code == 400
        assert resp.json == {"error": "request body must be an object"}

    def test_ignores_non_playback(self, client):
        resp = client.post(
            "/webhook/jellyfin",
            json={"NotificationType": "ItemAdded", "ItemId": "123"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json["status"] == "ignored"

    def test_requires_item_id(self, client):
        resp = client.post(
            "/webhook/jellyfin",
            json={"NotificationType": "PlaybackStart"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json["status"] == "no item"

    def test_requires_api_key(self, client):
        with patch.object(cache_app, "JELLYFIN_API_KEY", ""):
            resp = client.post(
                "/webhook/jellyfin",
                json={"NotificationType": "PlaybackStart", "ItemId": "abc"},
                content_type="application/json",
            )
            assert resp.json["status"] == "no jellyfin api key"


class TestPlexWebhook:
    def test_ignores_non_play(self, client):
        resp = client.post(
            "/webhook/plex",
            data={"payload": json.dumps({"event": "media.stop"})},
        )
        assert resp.status_code == 200
        assert resp.json["status"] == "ignored"

    def test_requires_payload(self, client):
        resp = client.post("/webhook/plex")
        assert resp.json["status"] == "no payload"

    def test_handles_bad_json(self, client):
        resp = client.post("/webhook/plex", data={"payload": "not json"})
        assert resp.json["status"] == "bad json"

    def test_rejects_malformed_episode_indices(self, client):
        resp = client.post(
            "/webhook/plex",
            data={
                "payload": json.dumps(
                    {
                        "event": "media.play",
                        "Metadata": {
                            "type": "episode",
                            "grandparentTitle": "Test Show",
                            "parentIndex": "not-a-number",
                            "index": "2",
                        },
                    }
                )
            },
        )

        assert resp.status_code == 400
        assert resp.json == {"error": "invalid episode metadata"}

    @patch.object(cache_app, "_radarr_get")
    @patch.object(cache_app, "_resolve_sonarr_series")
    def test_episode_triggers_prefetch(self, mock_sonarr, mock_radarr, client):
        mock_sonarr.return_value = {"id": 1, "title": "Test Show"}
        with patch.object(
            cache_app,
            "_get_remaining_season_paths",
            return_value=["/tv/s01e02.mkv", "/tv/s01e03.mkv"],
        ):
            with (
                patch.object(cache_app, "_prefetch_priority_file") as mock_pri,
                patch.object(cache_app, "_prefetch_paths_background") as mock_bg,
            ):
                resp = client.post(
                    "/webhook/plex",
                    data={
                        "payload": json.dumps(
                            {
                                "event": "media.play",
                                "Metadata": {
                                    "type": "episode",
                                    "grandparentTitle": "Test Show",
                                    "parentIndex": "1",
                                    "index": "2",
                                },
                            }
                        )
                    },
                )
                assert resp.json["status"] == "prefetching season"
                mock_pri.assert_called_once_with("/tv/s01e02.mkv")
                mock_bg.assert_called_once_with(["/tv/s01e03.mkv"])

    @patch.object(cache_app, "_radarr_get")
    def test_movie_triggers_prefetch(self, mock_radarr, client):
        mock_radarr.return_value = [
            {
                "title": "Test Movie",
                "hasFile": True,
                "movieFile": {"path": "/movies/test.mkv"},
            }
        ]
        with (
            patch.object(cache_app, "_prefetch_priority_file") as mock_pri,
            patch.object(cache_app, "_save_state"),
        ):
            resp = client.post(
                "/webhook/plex",
                data={
                    "payload": json.dumps(
                        {
                            "event": "media.play",
                            "Metadata": {"type": "movie", "title": "Test Movie"},
                        }
                    )
                },
            )
            assert resp.json["status"] == "prefetching movie"
            mock_pri.assert_called_once_with("/movies/test.mkv")


class TestBackendsAPI:
    def test_admin_api_fails_closed_without_token(self, client):
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", ""):
            resp = client.post("/api/backends/rebuild-pool")

        assert resp.status_code == 503

    def test_admin_api_rejects_invalid_token(self, client):
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", "expected-token"):
            resp = client.post("/api/backends/rebuild-pool", headers={"X-Media-Cache-Token": "wrong-token"})

        assert resp.status_code == 401

    @patch("subprocess.run")
    def test_list_backends(self, mock_run, client):
        mock_run.return_value = MagicMock(stdout="b2:\nhetzner:\n", returncode=0)
        resp = client.get("/api/backends")
        assert resp.json["backends"] == ["b2", "hetzner"]

    @patch("subprocess.run")
    def test_add_backend(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", "test-token"):
            resp = client.post(
                "/api/backends/add",
                json={"name": "test-remote", "type": "s3", "params": {"provider": "AWS"}},
                content_type="application/json",
                headers={"X-Media-Cache-Token": "test-token"},
            )
        assert resp.status_code == 501
        assert resp.json == {
            "error": "backend configuration is deployment-owned",
            "detail": "configure rclone through the parent deployment controller",
        }
        mock_run.assert_not_called()

    def test_add_backend_requires_fields(self, client):
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", "test-token"):
            resp = client.post(
                "/api/backends/add",
                json={},
                content_type="application/json",
                headers={"X-Media-Cache-Token": "test-token"},
            )
        assert resp.status_code == 400

    def test_remove_backend_rejects_non_object_body(self, client):
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", "test-token"):
            resp = client.post(
                "/api/backends/remove",
                json=["remote"],
                headers={"X-Media-Cache-Token": "test-token"},
            )

        assert resp.status_code == 400
        assert resp.json == {"error": "request body must be an object"}

    @pytest.mark.parametrize("params", [["not", "an", "object"], "provider=AWS", None])
    def test_add_backend_requires_parameter_object(self, client, params):
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", "test-token"):
            resp = client.post(
                "/api/backends/add",
                json={"name": "test-remote", "type": "s3", "params": params},
                headers={"X-Media-Cache-Token": "test-token"},
            )

        assert resp.status_code == 400
        assert resp.json == {"error": "params must be an object"}

    def test_add_backend_rejects_unbounded_parameters(self, client):
        params = {f"key_{index}": "value" for index in range(cache_app.MAX_BACKEND_PARAMS + 1)}
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", "test-token"):
            resp = client.post(
                "/api/backends/add",
                json={"name": "test-remote", "type": "s3", "params": params},
                headers={"X-Media-Cache-Token": "test-token"},
            )

        assert resp.status_code == 400

    def test_add_backend_rejects_nested_parameter_values(self, client):
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", "test-token"):
            resp = client.post(
                "/api/backends/add",
                json={"name": "test-remote", "type": "s3", "params": {"provider": {"nested": "value"}}},
                headers={"X-Media-Cache-Token": "test-token"},
            )

        assert resp.status_code == 400

    @patch("subprocess.run")
    def test_add_backend_hides_rclone_errors(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=1, stderr="secret_access_key=leaked")
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", "test-token"):
            resp = client.post(
                "/api/backends/add",
                json={"name": "test-remote", "type": "s3", "params": {"provider": "AWS"}},
                headers={"X-Media-Cache-Token": "test-token"},
            )

        assert resp.status_code == 501
        assert resp.json["error"] == "backend configuration is deployment-owned"
        mock_run.assert_not_called()

    @pytest.mark.parametrize("path", ["/api/backends/remove", "/api/backends/rebuild-pool"])
    @patch("subprocess.run")
    def test_backend_mutations_never_invoke_rclone(self, mock_run, client, path):
        with patch.object(cache_app, "MEDIA_CACHE_TOKEN", "test-token"):
            payload = {"name": "remote"} if path.endswith("remove") else None
            resp = client.post(path, json=payload, headers={"X-Media-Cache-Token": "test-token"})

        assert resp.status_code == 501
        assert resp.json["error"] == "backend configuration is deployment-owned"
        mock_run.assert_not_called()


class TestWatchState:
    def test_touch_tracked(self, client):
        with patch.object(cache_app, "LIBRARY_DIR", "/library"), patch.object(cache_app, "_save_state"):
            cache_app._touch_watched("/library/test/file.mkv")
        resp = client.get("/api/watch-state")
        assert resp.json["files"] >= 1
        assert "/library/test/file.mkv" in resp.json["state"]

    def test_pin_accepts_arr_namespace_path(self, client):
        with (
            patch.object(cache_app, "MEDIA_CACHE_TOKEN", "test-token"),
            patch.object(cache_app, "LIBRARY_DIR", "/library"),
            patch.object(cache_app, "_save_state"),
        ):
            resp = client.post(
                "/api/pin",
                json={"path": "/data/movies/film.mkv"},
                headers={"X-Media-Cache-Token": "test-token"},
            )

        assert resp.status_code == 200
        assert resp.json["path"] == "/library/movies/film.mkv"

    def test_load_state_discards_invalid_shape(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text('["not", "a", "mapping"]')

        with patch.object(cache_app, "STATE_FILE", str(state_file)):
            cache_app._load_state()

        assert cache_app._watch_state == {}

    def test_load_state_quarantines_corrupt_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{not-json")

        with patch.object(cache_app, "STATE_FILE", str(state_file)):
            cache_app._load_state()

        assert cache_app._watch_state == {}
        assert not state_file.exists()
        assert list(tmp_path.glob("state.json.corrupt-*"))


class TestEviction:
    def test_eviction_skips_pinned(self):
        cache_app._watch_state = {
            "/pinned.mkv": {"last_watched": "2020-01-01T00:00:00", "pinned": True},
        }
        with patch("os.path.exists", return_value=False):
            cache_app._eviction_check()
        assert "/pinned.mkv" in cache_app._watch_state

    def test_eviction_removes_missing_files(self):
        cache_app._watch_state = {
            "/gone.mkv": {"last_watched": "2020-01-01T00:00:00", "pinned": False},
        }
        with (
            patch("os.path.exists", return_value=False),
            patch.object(cache_app, "_save_state"),
        ):
            cache_app._eviction_check()
        assert "/gone.mkv" not in cache_app._watch_state

    def test_eviction_discards_invalid_timestamp(self):
        cache_app._watch_state = {
            "/library/broken.mkv": {"last_watched": "not-a-date", "pinned": False},
        }
        with patch.object(cache_app, "_save_state"):
            cache_app._eviction_check()

        assert "/library/broken.mkv" not in cache_app._watch_state

    def test_eviction_keeps_state_when_cache_delete_fails(self, tmp_path):
        library = tmp_path / "library"
        cache = tmp_path / "cache"
        media = library / "movie.mkv"
        cached = cache / "movie.mkv"
        library.mkdir()
        cache.mkdir()
        media.write_bytes(b"library")
        cached.write_bytes(b"cached")
        cache_app._watch_state = {
            str(media): {"last_watched": "2020-01-01T00:00:00+00:00", "pinned": False},
        }

        with (
            patch.object(cache_app, "LIBRARY_DIR", str(library)),
            patch.object(cache_app, "CACHE_DIR", str(cache)),
            patch.object(cache_app, "_save_state"),
            patch.object(type(cached), "unlink", side_effect=OSError("busy")),
        ):
            cache_app._eviction_check()

        assert str(media) in cache_app._watch_state
        assert cached.exists()


class TestPrefetchCoordination:
    def test_background_prefetch_reserves_work_before_submission(self):
        cache_app._prefetch_active.clear()
        pool = MagicMock()
        with (
            patch.object(cache_app, "_translate_to_library", return_value="/library/episode.mkv"),
            patch.object(cache_app, "_touch_watched"),
            patch.object(cache_app, "_prefetch_pool", pool),
        ):
            cache_app._prefetch_paths_background(["/data/episode.mkv"])
            cache_app._prefetch_paths_background(["/data/episode.mkv"])

        pool.submit.assert_called_once_with(
            cache_app._prefetch_full_file,
            "/library/episode.mkv",
            True,
        )


def test_scheduler_prevents_overlapping_eviction_jobs():
    scheduler = MagicMock()
    with patch.object(cache_app, "BackgroundScheduler", return_value=scheduler):
        cache_app._start_scheduler()

    scheduler.add_job.assert_called_once_with(
        cache_app._eviction_check,
        "interval",
        hours=6,
        id="eviction",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.start.assert_called_once()
