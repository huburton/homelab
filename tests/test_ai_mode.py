"""Runs ai/ai-mode for real with stub commands (wallpaper-video, notify-send, curl) in a temp runtime dir."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ai" / "ai-mode"


@pytest.fixture()
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    for name in ("wallpaper-video", "notify-send", "curl"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\n')
        stub.chmod(0o755)
    (tmp_path / "key").write_text("k")
    e = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", XDG_RUNTIME_DIR=str(tmp_path),
             HOME=str(tmp_path), LLM_KEY_FILE=str(tmp_path / "key"))
    return e, calls


def run(env, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], env=env[0], capture_output=True, text=True, timeout=20)


def test_on_off_cycle(env):
    assert run(env, "status").stdout.strip() == "off"
    assert run(env, "on").returncode == 0
    assert run(env, "status").stdout.strip() == "on"
    assert "wallpaper-video pause" in env[1].read_text()
    assert run(env, "off").returncode == 0
    assert run(env, "status").stdout.strip() == "off"
    log = env[1].read_text()
    assert "wallpaper-video resume" in log
    assert "/models" in log  # asked the router which models to unload before giving VRAM back


def test_toggle(env):
    run(env, "toggle")
    assert run(env, "status").stdout.strip() == "on"
    run(env, "toggle")
    assert run(env, "status").stdout.strip() == "off"


def test_on_is_idempotent_and_notifies_once(env):
    run(env, "on")
    run(env, "on")
    assert env[1].read_text().count("notify-send") == 1
    assert env[1].read_text().count("wallpaper-video pause") == 2  # still re-checks reality


def test_bad_usage(env):
    r = run(env, "bogus")
    assert r.returncode == 1 and "Usage" in r.stderr
