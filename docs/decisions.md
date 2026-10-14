# Decisions

Short notes on why things are the way they are. Each one states the context, the choice and the cost.

## 1. Cloudflare Tunnel in front of Caddy

- **Context**: residential connection; I did not want to forward ports on the home router or depend on a fixed IP.
- **Choice**: `cloudflared` runs as a container on `caddy-net` and keeps an outbound connection to Cloudflare.
  Public hostnames point at `http://caddy:80`; Caddy routes by hostname to containers. TLS ends at Cloudflare.
  The dashboard additionally sits behind Cloudflare Access (one-time code by e-mail).
- **Cost**: Cloudflare sees the decrypted traffic and is a dependency for public access. Services that should
  not be public at all (Portainer, Syncthing UI, Nextcloud) are reached on the LAN or through Tailscale.

## 2. ntfy with `auth-default-access: deny-all`

- **Context**: the ntfy container ran for a while with no config file at all, so anybody who guessed the topic
  name could read the notifications, and they started to carry enrolment codes for my own apps.
- **Choice**: deny-all by default; one write-only user + token per publisher (dashboard API, assistant), one
  read/write account for the phone. Other scripts go through the dashboard's `POST /api/notify` rather than
  holding their own token.
- **Cost**: adding a publisher takes three `ntfy` commands (see `stacks/ntfy/server.yml.example`).

## 3. One model at a time on the desktop, and "all in VRAM or nothing"

- **Context**: a 27B model at IQ3_XXS needs ~15.5 GB of the RTX 5080's 16 GB with its KV cache. The animated
  wallpaper (mpvpaper, 4K video) holds ~1.5 GB. With llama.cpp's default `--fit`, the model loaded anyway, half on
  the CPU, and the desktop froze.
- **Choice**: `--models-max 1` and, for the 27B presets, `fit = off` + `n-gpu-layers = 999`: if the VRAM is not
  free, the load fails immediately and clients fall back to the server model. Models unload after 10 minutes
  idle (`sleep-idle-seconds = 600`).
- **Cost**: the big model is only available when the desktop is in AI mode.

## 4. VRAM juggling with `ai-mode`

- **Choice**: `ai-mode on` pauses the wallpaper service (static image); `ai-mode off` unloads the router's models
  first, then resumes the wallpaper, which itself waits for enough free VRAM (otherwise mpv silently falls back to
  CPU decoding of a 4K video).
- Switches are serialized with `flock`: an `on` arriving during a ~15 s `off` used to leave the flag "on" with
  the wallpaper running. `on` is idempotent on the real state, not only on the flag.
- **Automatic AI mode**: `pc-status` switches AI mode on after 10 minutes of user absence (screensaver running)
  so batch jobs can use the 27B, and switches it back off when the user returns, only if it was the one that
  switched it on. A manual AI mode is never touched. The decision is a pure function (`auto_step`) with tests.
- `claude-local` turns AI mode on for its session and off at the end, unless another local session still runs.

## 5. Server: a 35B MoE with experts in RAM instead of a small dense model

- **Context**: the server GPU has 6 GB. A dense 4B model fit, but its French was weak.
- **Choice**: Qwen3.6-35B-A3B (mixture of experts, ~3B active parameters) with `n-cpu-moe = 99`: the expert
  weights stay in system RAM, attention and KV cache on the GPU (~2.9 GB VRAM). Measured with `ai/bench.py`:
  21 tok/s (33 with reasoning), ~125 tok/s prompt processing, 3/3 tool calls, valid JSON-schema output.
  The embedding model runs on the CPU so it does not compete for VRAM.
- **Cost**: RAM. llama.cpp's prompt cache defaults to 8 GB; it is capped at 1 GB (`cache-ram = 1024`) because the
  same 31 GB also host the Minecraft server (~9.5 GB) and the other containers.

## 6. A patched chat template

- Qwen's chat template raises an error on a `system` message that is not the first message. Claude Code sends
  such messages mid-conversation. `ai/templates/qwen-lenient.jinja` renders them as a normal system turn
  instead (one `if`, marked "Local patch").
- `claude-local` also sets `CLAUDE_CODE_ATTRIBUTION_HEADER=0`: the changing header would invalidate llama.cpp's
  KV cache on every request.

## 7. The desktop's home on NFS, exported `async`

- **Context**: one home directory whichever machine I sit at, backed up with the rest of the server.
- The export started `sync`. On a single SATA SSD under load, small-file creation took ~235 ms and the desktop
  stalled regularly. `async` brought it to ~0.3 ms. Accepted risk: a server crash may lose the last seconds of
  writes; only one machine writes at a time. The notes vault, which several sync tools write to, stays `sync`.

## 8. Plain compose files in git

- On the live server several stacks were created through Portainer's web editor, with secrets inline. This
  repository is the cleaned-up export: one `compose.yml` per stack, secrets as `${VARS}` from `.env`, validated in
  CI with `docker compose config -q`. Portainer stays as a UI for logs and restarts.
