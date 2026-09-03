"""Storage safety tests: path traversal, sanitization, atomicity (P10-20, P10-21)."""
import json
import os
import pytest

from app.core.config import reset_config
from app.core.storage import (PathEscapeError, StorageManager,
                              sanitize_filename)


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("ARS_CONFIG", str(tmp_path / "config.json"))
    tmp_path.joinpath("config.json").write_text(json.dumps({"storage": {"root": str(tmp_path / "storage")}}))
    reset_config()
    import app.core.config as cfg
    cfg._config = cfg.load_config()
    yield StorageManager()
    reset_config()


def test_sanitize_strips_traversal():
    for evil in ("../../etc/passwd", "..\\..\\windows\\system32\\cmd.exe",
                 "/home/user/.ssh/id_rsa", "C:\\Users\\ahmed\\Desktop\\x.mp4"):
        out = sanitize_filename(evil)
        assert "/" not in out and "\\" not in out
        assert ".." not in out


def test_sanitize_control_chars_and_reserved():
    assert sanitize_filename("con") == "_con"
    assert sanitize_filename("COM1.mp4") == "_COM1.mp4"
    assert "\x00" not in sanitize_filename("bad\x00name.mp4")
    assert sanitize_filename("") != ""


def test_sanitize_fuzz():  # P10-21
    import random
    rng = random.Random(42)
    alphabet = "../\\:\x00\x1f%%{}$|;&<>\"'`*?[]"
    for _ in range(500):
        s = "".join(rng.choice(alphabet + "abcMOV0.") for _ in range(rng.randint(0, 30)))
        out = sanitize_filename(s)
        assert out != ""
        assert "/" not in out and "\\" not in out and "\x00" not in out
        assert not out.startswith(".")


def test_path_traversal_blocked(storage):
    with pytest.raises(PathEscapeError):
        storage.safe_resolve("exports", "..", "..", "etc", "passwd")
    with pytest.raises(PathEscapeError):
        storage.safe_resolve("projects", "proj/../../..")


def test_symlink_escape_blocked(storage, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = storage.roots["exports"] / "evil"
    os.symlink(outside, link)
    with pytest.raises(PathEscapeError):
        storage.safe_resolve("exports", "evil", "file.txt")


def test_atomic_write_json(storage, tmp_path):
    p = storage.roots["projects"] / "p1" / "project.json"
    data = {"version": 1, "layers": [1, 2, 3]}
    storage.atomic_write_json(p, data)
    assert json.loads(p.read_text()) == data
    # overwrite is atomic; no leftover temp files
    storage.atomic_write_json(p, {"version": 2})
    assert json.loads(p.read_text())["version"] == 2
    leftovers = [f for f in p.parent.iterdir() if f.name.startswith(".")]
    assert leftovers == []


def test_free_space_and_output_dir(storage):
    assert storage.free_space("exports") > 0
    ok, _ = storage.check_output_dir("exports")
    assert ok


def test_temp_sweep(storage):
    d = storage.roots["temp"] / "old_job"
    d.mkdir(parents=True)
    f = d / "x.bin"
    f.write_bytes(b"x")
    old = f.stat().st_mtime - 48 * 3600
    os.utime(f, (old, old))
    os.utime(d, (old, old))
    removed = storage.sweep_temp(24.0)
    assert removed >= 1
    assert not d.exists()


def test_remove_tree_refuses_outside(storage, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises((PathEscapeError, Exception)):
        storage.remove_tree(outside)
    with pytest.raises(Exception):
        storage.remove_tree(storage.roots["exports"])  # never remove a root itself
