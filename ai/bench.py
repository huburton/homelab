#!/usr/bin/env python3
"""Model benchmark: start an isolated llama-server (port 8091), measure VRAM, speed and quality, then stop it.
Usage: bench.py <label> <model.gguf> [extra llama-server args...]

Checks: generation speed with and without reasoning (plus speculative-decoding acceptance), tool calls (3 cases),
strict JSON-schema extraction, and "language drift" (does a French prompt get an English answer?).
The prompts are in French on purpose: the models serve a French-speaking user.
Writes <label>.json and <label>.server.log next to this script (or in BENCH_OUT)."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

LABEL, MODEL, EXTRA = sys.argv[1], sys.argv[2], sys.argv[3:]
PORT = int(os.environ.get("BENCH_PORT", 8091))
SERVER = os.environ.get("BENCH_SERVER", "/opt/llama.cpp/current/llama-server")
OUTDIR = os.environ.get("BENCH_OUT", os.path.dirname(os.path.abspath(__file__)))
# Base options (JSON list in BENCH_BASE); model, host and port are added here
BASE = [SERVER, "--model", MODEL, "--host", "127.0.0.1", "--port", str(PORT), "--jinja"] + json.loads(
    os.environ.get("BENCH_BASE", "[]"))


def vram():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.split(",")
    return int(out[0]), int(out[1])


def post(path, body, timeout=600):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def chat(messages, think=False, **kw):
    body = {"messages": messages, "chat_template_kwargs": {"enable_thinking": think}, **kw}
    t = time.time()
    r = post("/v1/chat/completions", body)
    r["_wall"] = time.time() - t
    return r


def speed(r):
    t = r.get("timings", {})
    return round(t.get("predicted_per_second", 0), 1), t.get("predicted_n"), \
        (round(t["draft_n_accepted"] / t["draft_n"], 2) if t.get("draft_n") else None)


before = vram()[0]
log = open(f"{OUTDIR}/{LABEL}.server.log", "w")
proc = subprocess.Popen(BASE + EXTRA, stdout=log, stderr=subprocess.STDOUT)
res = {"label": LABEL, "model": MODEL.rsplit("/", 1)[-1], "extra": " ".join(EXTRA), "vram_before_mb": before}
try:
    t0 = time.time()
    while True:
        if proc.poll() is not None:
            raise SystemExit(f"llama-server died (code {proc.returncode}), see {log.name}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                if json.load(r).get("status") == "ok":
                    break
        except Exception:
            pass
        if time.time() - t0 > 300:
            raise SystemExit("load timeout")
        time.sleep(1)
    res["load_s"] = round(time.time() - t0, 1)
    res["vram_loaded_mb"], res["vram_total_mb"] = vram()
    res["ram_available_mb"] = int(next(l for l in open("/proc/meminfo") if l.startswith("MemAvailable")).split()[1]) // 1024

    # 1. Speed without reasoning: long French text
    r = chat([{"role": "user", "content": "Explique en français, en environ 400 mots, comment fonctionne un serveur NFS "
               "et pourquoi un export « sync » peut figer un bureau Linux. Sois concret."}], max_tokens=700)
    res["speed_nothink_tok_s"], res["speed_nothink_n"], res["speed_nothink_accept"] = speed(r)
    res["sample_fr"] = r["choices"][0]["message"]["content"][:700]

    # 2. Speed with reasoning (short problem)
    r = chat([{"role": "user", "content": "Un train part à 14h12 et roule 2h47 avec 18 min d'arrêt. "
               "À quelle heure arrive-t-il ? Réponds juste l'heure."}], think=True, max_tokens=3000)
    res["speed_think_tok_s"], res["speed_think_n"], res["speed_think_accept"] = speed(r)
    res["think_answer"] = r["choices"][0]["message"]["content"].strip()[-80:]

    # 3. Tool calls (OpenAI format)
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "Météo prévue pour une ville",
              "parameters": {"type": "object", "properties": {"city": {"type": "string"},
                             "day": {"type": "string", "enum": ["today", "tomorrow"]}}, "required": ["city", "day"]}}},
             {"type": "function", "function": {"name": "set_volume", "description": "Règle le volume système (0-100)",
              "parameters": {"type": "object", "properties": {"level": {"type": "integer"}}, "required": ["level"]}}}]
    ok = 0
    cases = [("Il va faire quel temps demain à Nancy ?", "get_weather", lambda a: "nancy" in a.get("city", "").lower() and a.get("day") == "tomorrow"),
             ("Mets le son à 35", "set_volume", lambda a: a.get("level") == 35),
             ("Météo d'aujourd'hui à Luxembourg stp", "get_weather", lambda a: "luxem" in a.get("city", "").lower() and a.get("day") == "today")]
    detail = []
    for q, name, check in cases:
        r = chat([{"role": "user", "content": q}], tools=tools, max_tokens=300)
        calls = r["choices"][0]["message"].get("tool_calls") or []
        good = bool(calls) and calls[0]["function"]["name"] == name and check(json.loads(calls[0]["function"]["arguments"] or "{}"))
        ok += good
        detail.append(f"{'OK' if good else 'KO'} {q} -> {[c['function']['name'] + c['function']['arguments'] for c in calls]}")
    res["tools"] = f"{ok}/{len(cases)}"
    res["tools_detail"] = detail

    # 4. Strict JSON extraction (job-offer scoring style)
    schema = {"type": "object", "properties": {
        "titre": {"type": "string"}, "entreprise": {"type": "string"}, "pays": {"type": "string", "enum": ["LU", "BE", "FR", "autre"]},
        "teletravail": {"type": "boolean"}, "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "raison": {"type": "string"}}, "required": ["titre", "entreprise", "pays", "teletravail", "score", "raison"]}
    offre = ("Offre : Administrateur systèmes Linux (H/F) chez ACME SA, Luxembourg. CDI, 2 jours de télétravail "
             "par semaine. Compétences : Linux, Docker, Ansible, supervision. Français et anglais requis.")
    profil = "Profil : développeur/admin Linux junior (Docker, NFS, llama.cpp), habite en Lorraine, cherche un emploi IT au Luxembourg."
    r = chat([{"role": "user", "content": f"{profil}\n{offre}\nExtrais les champs et note l'adéquation 0-100."}],
             response_format={"type": "json_schema", "json_schema": {"name": "offre", "schema": schema, "strict": True}},
             max_tokens=400)
    try:
        j = json.loads(r["choices"][0]["message"]["content"])
        res["json"] = "OK" if j.get("pays") == "LU" and j.get("teletravail") is True and "acme" in j.get("entreprise", "").lower() else "KO"
        res["json_value"] = j
    except Exception as e:
        res["json"] = f"KO ({e})"

    # 5. Language drift (reasoning on): answers or thoughts that switch to English
    FR = re.compile(r"\b(le|la|les|des|est|une|pour|avec|dans|vous|tu|et|que|pas)\b", re.I)
    EN = re.compile(r"\b(the|and|is|are|with|for|you|your|this|that|of|to)\b", re.I)
    prompts = [
        "Explique-moi simplement la différence entre un conteneur Docker et une machine virtuelle.",
        "Rédige un court message pour relancer un recruteur qui ne m'a pas répondu depuis une semaine.",
        "Quels sont les avantages et inconvénients de travailler au Luxembourg en habitant en France ?",
        "Mon PC sous Arch Linux ne s'éteint plus quand je clique sur Éteindre. Quelles pistes vérifier ?",
        "Résume en trois phrases l'intérêt de l'impression 3D pour un petit atelier.",
        "Donne-moi un plan de journée pour chercher un emploi efficacement.",
    ]
    if os.environ.get("BENCH_DRIFT", "1") == "0":
        prompts = []
    en_answers, en_think, empty = 0, 0, 0
    for p in prompts:
        m = chat([{"role": "user", "content": p}], think=True, max_tokens=4000)["choices"][0]["message"]
        ans, think = m.get("content") or "", m.get("reasoning_content") or ""
        if not ans.strip():
            empty += 1
        elif len(EN.findall(ans)) > len(FR.findall(ans)):
            en_answers += 1
        if think and len(EN.findall(think)) > len(FR.findall(think)):
            en_think += 1
    res["drift"] = f"EN answers {en_answers}/{len(prompts)}, EN thoughts {en_think}/{len(prompts)}, empty {empty}/{len(prompts)}"
finally:
    proc.terminate()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()

print(json.dumps(res, ensure_ascii=False, indent=1))
with open(f"{OUTDIR}/{LABEL}.json", "w") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
