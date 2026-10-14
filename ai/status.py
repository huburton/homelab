#!/usr/bin/env python3
"""ai-status / pc-status: state and control of one machine's local AI, over HTTP.

The same script runs on both machines:
  - on the desktop as the user unit `pc-status` (llama.cpp router on :8080, `ai-mode` available);
  - on the server as the user unit `ai-status` (router on :8095, no `ai-mode`).
It is read by the personal dashboard (AI page) and by the job-search agent (to pick a model).

HTTP on AI_STATUS_HOST:AI_STATUS_PORT (default 0.0.0.0:8097), protected by the local router's API key (Bearer):
  GET  /status                              ai mode, user idle, GPU, router models, services
  POST /ai-mode   {"on": bool}              (desktop only: runs `ai-mode on|off`)
  POST /llm/load  {"model": "qwen3.5-9b"}   POST /llm/unload {"model": "..."}

Settings (environment): AI_STATUS_ROUTER, AI_STATUS_KEY_FILE, AI_STATUS_HOST, AI_STATUS_PORT,
AI_STATUS_SERVICES ("name:user" or "name:system", comma separated), AI_STATUS_AUTO (0 = no automatic AI mode),
AI_STATUS_AUTO_AFTER (seconds of idle before automatic AI mode).

Demo: `python ai/status.py --mock` (or MOCK_GPU=1) serves fake GPU/model data on 127.0.0.1:8097 with the key
"demo"; no GPU, router, systemd or key file needed.
"""
import glob
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
MOCK = "--mock" in sys.argv or os.environ.get("MOCK_GPU") == "1"
KEY_FILE = Path(os.environ.get("AI_STATUS_KEY_FILE", HOME / ".config/llm/api-key"))
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
ROUTER = os.environ.get("AI_STATUS_ROUTER", "http://127.0.0.1:8080")
HOST = os.environ.get("AI_STATUS_HOST", "127.0.0.1" if MOCK else "0.0.0.0")
PORT = int(os.environ.get("AI_STATUS_PORT", "8097"))
SERVICES = [s.split(":") for s in os.environ.get("AI_STATUS_SERVICES", "llama-router:user").split(",") if s]
AI_MODE = Path(os.environ.get("AI_STATUS_AI_MODE", HOME / ".local/bin/ai-mode"))  # absent on the server
# Optional screensaver script: restarted in its low-VRAM variant when AI mode is switched on automatically.
SCREENSAVER = Path(os.environ.get("AI_STATUS_SCREENSAVER", HOME / ".local/bin/oled-screensaver"))
IDLE_FILE = Path(os.environ.get("AI_STATUS_IDLE_FILE", RUNTIME / "oled-screensaver.pids"))
AI_MODE_FLAG = RUNTIME / "ai-mode"
AUTO_FLAG = RUNTIME / "ai-mode.auto"  # AI mode switched on by this service (user away): undone when they return
AUTO_AFTER_S = int(os.environ.get("AI_STATUS_AUTO_AFTER", "600"))
NVIDIA_QUERY = ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits"]


def load_key() -> str:
    if MOCK:
        return os.environ.get("AI_STATUS_KEY", "demo")
    return os.environ.get("AI_STATUS_KEY") or KEY_FILE.read_text().strip()


# ---------------------------------------------------------------------------------------------------------------
# Pure helpers (unit-tested)

def parse_gpu(csv_line: str) -> dict | None:
    """One line of `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` -> dict, None if unparsable."""
    try:
        q = [part.strip() for part in csv_line.strip().splitlines()[0].split(",")]
        return {"name": q[0], "util": int(q[1]), "vram_used_mib": int(q[2]), "vram_total_mib": int(q[3]),
                "temp_c": int(q[4]), "power_w": round(float(q[5]))}
    except (IndexError, ValueError):
        return None


def parse_models(router_models: dict) -> list[dict]:
    """llama.cpp router `GET /models` payload -> [{"id", "status"}]."""
    return [{"id": m["id"], "status": (m.get("status") or {}).get("value")} for m in router_models.get("data", [])]


def authorized(header: str | None, key: str) -> bool:
    return bool(key) and header == f"Bearer {key}"


def auto_step(idle: bool, ai_mode_on: bool, auto_flag: bool, idle_since: float | None, now: float,
              after_s: int = AUTO_AFTER_S) -> tuple[str | None, float | None]:
    """One tick of the automatic AI mode. Returns (action, new idle_since), action in {None, "on", "off"}.

    - user idle for `after_s` and AI mode off -> "on" (the big model then fits in VRAM, batch jobs use it);
    - user back and the AI mode was switched on by us -> "off". A manually enabled AI mode is never touched.
    After an "on" the idle timer restarts, so a failed switch is retried only after another full delay.
    """
    if idle:
        idle_since = idle_since if idle_since is not None else now
        if not ai_mode_on and now - idle_since >= after_s:
            return "on", now
        return None, idle_since
    return ("off" if auto_flag else None), None


# ---------------------------------------------------------------------------------------------------------------
# Real backends

def _env() -> dict:
    env = dict(os.environ)
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):  # service started outside the graphical session
        sigs = sorted(glob.glob(str(RUNTIME / "hypr" / "*")), key=os.path.getmtime)
        if sigs:
            env["HYPRLAND_INSTANCE_SIGNATURE"] = os.path.basename(sigs[-1])
    env.setdefault("WAYLAND_DISPLAY", "wayland-1")
    return env


def _router(path: str, body: dict | None = None):
    req = urllib.request.Request(ROUTER + path, headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


def _idle() -> bool:
    # The screensaver writes its pids while it runs: running = user away for at least a couple of minutes.
    return IDLE_FILE.exists() and IDLE_FILE.stat().st_size > 0


def auto_ai_mode(sleep=time.sleep, clock=time.monotonic):
    since = None
    while True:
        sleep(30)
        try:
            action, since = auto_step(_idle(), AI_MODE_FLAG.exists(), AUTO_FLAG.exists(), since, clock())
            if action == "on":
                subprocess.run([str(AI_MODE), "on"], env=_env(), timeout=60)
                AUTO_FLAG.touch()
                if SCREENSAVER.exists():  # restart it in its AI-mode variant (software decode, less VRAM)
                    subprocess.run([str(SCREENSAVER), "stop"], env=_env(), timeout=30)
                    subprocess.run([str(SCREENSAVER), "start"], env=_env(), timeout=30)
            elif action == "off":
                AUTO_FLAG.unlink(missing_ok=True)
                subprocess.Popen([str(AI_MODE), "off"], env=_env(), start_new_session=True)
        except Exception as e:
            print(f"auto AI mode: {e}", flush=True)


def status() -> dict:
    out = {"host": os.uname().nodename, "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "uptime_s": int(float(Path("/proc/uptime").read_text().split()[0])),
           "ai_mode": AI_MODE_FLAG.exists() if AI_MODE.exists() else None}
    out["user_idle"] = _idle() if AI_MODE.exists() else None
    out["ai_mode_auto"] = AUTO_FLAG.exists() if AI_MODE.exists() else None
    try:
        out["gpu"] = parse_gpu(subprocess.run(NVIDIA_QUERY, capture_output=True, text=True, timeout=5).stdout)
    except Exception:
        out["gpu"] = None
    try:
        out["models"] = parse_models(_router("/models"))
    except Exception:
        out["models"] = None  # router stopped
    out["services"] = {name: subprocess.run(["systemctl", *(["--user"] if scope == "user" else []), "is-active", name],
                                            capture_output=True, text=True).stdout.strip() for name, scope in SERVICES}
    return out


def set_ai_mode(on: bool) -> bool:
    if not AI_MODE.exists():
        return False
    subprocess.Popen([str(AI_MODE), "on" if on else "off"], env=_env(), start_new_session=True)
    return True


def llm_action(action: str, model: str) -> dict:
    return _router("/models/" + action, {"model": model})


# ---------------------------------------------------------------------------------------------------------------
# Mock backend (demo, tests): same payload shape, fake data, state kept in memory

class Mock:
    def __init__(self):
        self.ai_mode = False
        self.models = {"qwen3.8-27b": "unloaded", "qwen3.8-27b-long": "unloaded", "qwen3.5-9b": "loaded"}
        self.sizes = {"qwen3.8-27b": 15700, "qwen3.8-27b-long": 15300, "qwen3.5-9b": 7800}

    def status(self) -> dict:
        used = 600 + (0 if self.ai_mode else 1500) + sum(self.sizes[m] for m, s in self.models.items() if s == "loaded")
        return {"host": "desktop-demo", "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "uptime_s": 3600,
                "ai_mode": self.ai_mode, "user_idle": False, "ai_mode_auto": False,
                "gpu": parse_gpu(f"NVIDIA GeForce RTX 5080 (mock), 3, {min(used, 16303)}, 16303, 41, 38.2"),
                "models": [{"id": m, "status": s} for m, s in self.models.items()],
                "services": {"llama-router": "active"}}

    def set_ai_mode(self, on: bool) -> bool:
        self.ai_mode = on
        return True

    def llm_action(self, action: str, model: str) -> dict:
        if model not in self.models:
            raise ValueError(f"unknown model {model}")
        if action == "load" and model.startswith("qwen3.8-27b") and not self.ai_mode:
            # fit = off in the presets: without AI mode the wallpaper holds VRAM and the load fails cleanly
            raise RuntimeError("not enough free VRAM for qwen3.8-27b: switch AI mode on first")
        if action == "load":  # the desktop router keeps one model at a time (--models-max 1)
            self.models = {m: "unloaded" for m in self.models}
        self.models[model] = "loaded" if action == "load" else "unloaded"
        return {"success": True}


class Real:
    status = staticmethod(status)
    set_ai_mode = staticmethod(set_ai_mode)
    llm_action = staticmethod(llm_action)


BACKEND = Mock() if MOCK else Real()
KEY = ""  # loaded in main(); tests set it directly


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if authorized(self.headers.get("Authorization"), KEY):
            return True
        self._json(401, {"error": "API key required"})
        return False

    def log_message(self, *args):
        pass

    def do_GET(self):
        if not self._authorized():
            return
        if self.path == "/status":
            return self._json(200, BACKEND.status())
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid JSON"})
        if self.path == "/ai-mode":
            if BACKEND.set_ai_mode(bool(body.get("on"))):
                return self._json(202, {"ok": True})
        if self.path in ("/llm/load", "/llm/unload") and body.get("model"):
            try:
                return self._json(200, BACKEND.llm_action(self.path.rsplit("/", 1)[1], body["model"]))
            except Exception as e:
                return self._json(502, {"error": str(e)})
        self._json(404, {"error": "not found"})


def main():
    global KEY
    KEY = load_key()
    if not MOCK and AI_MODE.exists() and os.environ.get("AI_STATUS_AUTO", "1") != "0":
        threading.Thread(target=auto_ai_mode, daemon=True).start()
    if MOCK:
        print(f"mock mode: http://{HOST}:{PORT}/status  (Authorization: Bearer {KEY})", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
