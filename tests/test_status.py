"""Unit tests for ai/status.py: pure helpers + the HTTP API against the mock backend."""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai"))
import status  # noqa: E402


# --- nvidia-smi / router parsing -------------------------------------------------------------------------------

def test_parse_gpu_nominal():
    gpu = status.parse_gpu("NVIDIA GeForce RTX 2060, 12, 2934, 6144, 47, 31.85\n")
    assert gpu == {"name": "NVIDIA GeForce RTX 2060", "util": 12, "vram_used_mib": 2934,
                   "vram_total_mib": 6144, "temp_c": 47, "power_w": 32}


def test_parse_gpu_takes_first_gpu_only():
    out = "GPU A, 1, 100, 1000, 30, 10.0\nGPU B, 2, 200, 2000, 40, 20.0\n"
    assert status.parse_gpu(out)["name"] == "GPU A"


@pytest.mark.parametrize("bad", ["", "NVIDIA-SMI has failed because it couldn't communicate with the driver",
                                 "GPU, [N/A], 1, 2, 3, 4"])
def test_parse_gpu_garbage_returns_none(bad):
    assert status.parse_gpu(bad) is None


def test_parse_models():
    payload = {"data": [{"id": "qwen3.5-9b", "status": {"value": "loaded"}}, {"id": "embed"}]}
    assert status.parse_models(payload) == [{"id": "qwen3.5-9b", "status": "loaded"}, {"id": "embed", "status": None}]


def test_authorized():
    assert status.authorized("Bearer s3cret", "s3cret")
    assert not status.authorized("Bearer nope", "s3cret")
    assert not status.authorized(None, "s3cret")
    assert not status.authorized("Bearer ", "")  # an empty key never authorizes


# --- automatic AI mode decision ---------------------------------------------------------------------------------

def test_auto_waits_for_the_full_idle_delay():
    action, since = status.auto_step(idle=True, ai_mode_on=False, auto_flag=False, idle_since=None, now=100, after_s=600)
    assert (action, since) == (None, 100)
    action, since = status.auto_step(True, False, False, since, now=699, after_s=600)
    assert action is None
    action, since = status.auto_step(True, False, False, since, now=700, after_s=600)
    assert (action, since) == ("on", 700)  # timer restarts: a failed switch is retried only after another delay


def test_auto_does_nothing_when_ai_mode_already_on():
    assert status.auto_step(True, True, False, idle_since=0, now=10_000, after_s=600)[0] is None


def test_auto_switches_off_only_what_it_switched_on():
    assert status.auto_step(idle=False, ai_mode_on=True, auto_flag=True, idle_since=5, now=10) == ("off", None)
    # manual AI mode (no auto flag): the user's choice is left alone
    assert status.auto_step(idle=False, ai_mode_on=True, auto_flag=False, idle_since=5, now=10) == (None, None)


# --- HTTP API with the mock backend -----------------------------------------------------------------------------

@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setattr(status, "BACKEND", status.Mock())
    monkeypatch.setattr(status, "KEY", "test-key")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), status.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def call(url, body=None, key="test-key"):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_status_requires_key(server):
    assert call(server + "/status", key="wrong")[0] == 401


def test_status_shape(server):
    code, data = call(server + "/status")
    assert code == 200
    assert {"host", "ai_mode", "gpu", "models", "services"} <= data.keys()
    assert data["gpu"]["vram_total_mib"] == 16303


def test_ai_mode_and_single_model_router(server):
    assert call(server + "/ai-mode", {"on": True})[0] == 202
    assert call(server + "/status")[1]["ai_mode"] is True
    assert call(server + "/llm/load", {"model": "qwen3.8-27b"})[0] == 200
    models = {m["id"]: m["status"] for m in call(server + "/status")[1]["models"]}
    assert [m for m, s in models.items() if s == "loaded"] == ["qwen3.8-27b"]  # one model at a time


def test_unknown_model_is_a_502(server):
    assert call(server + "/llm/load", {"model": "nope"})[0] == 502


def test_big_model_needs_ai_mode(server):
    code, data = call(server + "/llm/load", {"model": "qwen3.8-27b"})
    assert code == 502 and "AI mode" in data["error"]
