"""API + job + recovery integration tests (fast, no FFmpeg render)."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_config
from app.core.database import reset_db
from app.core.storage import reset_storage
from app.workers.job_manager import reset_job_manager


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "storage": {"root": str(tmp_path / "storage")},
        "logging": {"level": "WARNING"},
    }))
    monkeypatch.setenv("ARS_CONFIG", str(cfg_path))
    reset_config()
    reset_db()
    reset_storage()
    reset_job_manager()
    import app.core.recovery as recovery_mod
    recovery_mod._recovery = None
    from app.server import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_config()
    reset_db()
    reset_storage()
    reset_job_manager()
    recovery_mod._recovery = None


def test_health_and_system(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["local_only"] is True
    r = client.get("/api/system")
    assert r.status_code == 200
    assert "ffmpeg" in r.json() and "platform" in r.json()


def test_project_crud_roundtrip(client):
    r = client.post("/api/projects", json={"name": "Round Trip"})
    pid = r.json()["project_id"]
    doc = r.json()["project"]
    doc["layers"].append({"id": "l1", "type": "video", "geometry": {"x": 0, "y": 0, "w": 1, "h": 1}})
    doc["timeline"] = [
        {"layerId": "l1", "action": "play", "wallMs": 1000, "mediaTime": 0, "payload": None},
        {"layerId": "l1", "action": "pause", "wallMs": 3500, "mediaTime": 2.5, "payload": None},
    ]
    r = client.put(f"/api/projects/{pid}", json={"project": doc})
    assert r.json()["saved"] is True
    r = client.get(f"/api/projects/{pid}")
    loaded = r.json()["project"]
    assert loaded["timeline"] == doc["timeline"]          # zero-loss round trip (P6-E2)
    assert loaded["layers"] == doc["layers"]
    assert loaded["version"] == 1                          # versioned from day one (GR-20)
    # list
    r = client.get("/api/projects")
    assert any(p["id"] == pid for p in r.json()["projects"])
    # delete never touches sources (nothing to touch here — must simply succeed)
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_project_version_guard(client):
    r = client.post("/api/projects", json={"name": "x"})
    pid = r.json()["project_id"]
    doc = r.json()["project"]
    doc["version"] = 99
    r = client.put(f"/api/projects/{pid}", json={"project": doc})
    assert r.status_code == 422                            # refuse future versions cleanly (P6-20)


def test_media_upload_probe_delete(client, tmp_path):
    # build a tiny real video? not needed: any file probes via ffprobe which
    # may be missing in the test env — instead test register path validation
    bad = client.post("/api/media/register", json={"path": "/etc/passwd"})
    assert bad.status_code == 403                          # outside storage roots (GR-18)
    bad = client.post("/api/media/register", json={"path": str(tmp_path / "nope.mp4")})
    assert bad.status_code == 403                          # also outside roots
    # a path inside a storage root that does not exist -> 404
    from app.core.config import get_config
    inside = get_config().subroot("proxies") / "missing.mp4"
    r = client.post("/api/media/register", json={"path": str(inside)})
    assert r.status_code == 404
    missing = client.get("/api/media/media_nonexistent")
    assert missing.status_code == 404
    missing = client.delete("/api/media/media_nonexistent")
    assert missing.status_code == 404


def test_export_settings_validation(client):
    r = client.post("/api/export", json={
        "settings": {"format": "avi", "fps": 30}, "take_start_ms": 0, "take_end_ms": 5000})
    assert r.status_code == 422
    r = client.post("/api/export", json={
        "settings": {"format": "mp4", "fps": 30}, "take_start_ms": 0, "take_end_ms": 0})
    assert r.status_code == 422                            # zero duration refused


def test_recording_upload_and_meta(client):
    pid = client.post("/api/projects", json={"name": "rec"}).json()["project_id"]
    r = client.post(f"/api/recording/{pid}",
                    files={"file": ("composite.webm", b"FAKEWEBMDATA", "video/webm")},
                    data={"kind": "composite", "take_id": "take_1",
                          "wall_start_ms": "1000", "wall_end_ms": "7000"})
    assert r.status_code == 200 and r.json()["size"] > 0
    r = client.post(f"/api/recording/{pid}/take_1/meta", json={
        "duration_s": 6.0, "event_count": 12,
        "timeline": [{"layerId": "a", "action": "play", "wallMs": 0, "mediaTime": 0}]})
    assert r.status_code == 200
    takes = client.get(f"/api/recording/{pid}").json()["takes"]
    assert len(takes) == 1 and takes[0]["status"] == "COMPLETE"
    assert takes[0]["meta"]["duration_s"] == 6.0
    assert client.delete(f"/api/recording/{pid}/take_1").status_code == 200
    assert client.get(f"/api/recording/{pid}").json()["takes"] == []


def test_dirty_flag_and_recovery_pointer(client):
    pid = client.post("/api/projects", json={"name": "rec2"}).json()["project_id"]
    assert client.post(f"/api/projects/{pid}/dirty").status_code == 200
    from app.core.config import get_config
    from app.core.recovery import RecoveryManager
    rec = RecoveryManager(get_config())
    assert rec.is_dirty()
    result = rec.recover()
    assert result["recovered"] is True and result["project_id"] == pid
    assert not rec.is_dirty()      # clean after successful recovery


def test_recovery_attempt_cap(client, tmp_path):  # GR-13 / AC-17
    from app.core.config import get_config
    from app.core.recovery import RecoveryManager
    rec = RecoveryManager(get_config())
    rec.mark_dirty()
    for _ in range(rec.max_attempts + 2):
        rec.recover()
        rec.mark_dirty()
    # after cap: gives up cleanly, no infinite loop, originals untouched
    n = rec.recover()
    assert n["recovered"] is False
    assert "attempts" in n["message"].lower() or "fresh" in n["message"].lower()


def test_path_traversal_on_media_file(client):
    pid = client.post("/api/projects", json={"name": "x1"}).json()["project_id"]
    # no registered media -> 404 rather than any filesystem probing
    r = client.get("/api/media/media_x/file")
    assert r.status_code == 404
