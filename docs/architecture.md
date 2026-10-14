# Architecture

Two machines on the same LAN:

- **server**: always on. Arch Linux, Intel i5-12600KF, 31 GB RAM, RTX 2060 6 GB. Runs about 30 Docker
  containers, the server-side LLM router, and exports the desktop's home directory over NFS.
- **desktop**: the daily workstation and gaming PC, RTX 5080 16 GB. Its `/home` is mounted from the server; it runs
  its own LLM router for the big model when it is not busy.

## Network and services

```mermaid
flowchart LR
    user((Browser / phone)) -->|HTTPS| cf[Cloudflare edge<br/>TLS, Access on the dashboard]
    cf -->|outbound tunnel<br/>no port forwarding| cloudflared
    remote((Laptop / phone<br/>away from home)) -.->|Tailscale| server_lan

    subgraph server [server - Docker, network caddy-net]
        cloudflared --> caddy[Caddy<br/>reverse proxy]
        caddy --> vaultwarden
        caddy --> homeassistant[Home Assistant]
        caddy --> motioneye[MotionEye]
        caddy --> gitea[Gitea]
        caddy --> couchdb[CouchDB<br/>Obsidian LiveSync]
        caddy --> ntfy[ntfy<br/>deny-all]
        caddy --> dashboard[Dashboard + API]
        nextcloud[Nextcloud + MariaDB + Redis]
        syncthing[Syncthing]
        esphome[ESPHome<br/>host network]
        portainer[Portainer]
        minecraft[Minecraft]
        server_lan[LAN ports]
    end

    esphome -.->|mDNS| homeassistant
    dashboard -->|status / load / unload| aistatus[ai-status / pc-status<br/>see below]
```

Notes:
- Only Caddy is on the path from the Internet; every web service joins the external `caddy-net` network created
  by the Caddy stack and is addressed by container name.
- Portainer, Syncthing's UI, Nextcloud and the Minecraft server are reached on the LAN or through Tailscale,
  not through the tunnel.
- The dashboard is protected by Cloudflare Access (one-time code by e-mail) before traffic even reaches the tunnel.

## Local AI on two machines

```mermaid
flowchart TB
    subgraph S [server - RTX 2060 6 GB]
        rs[llama.cpp router :8095<br/>Qwen3.6-35B-A3B MoE 24/7, 32K<br/>experts in RAM, attention on GPU<br/>+ embeddings 0.6B on CPU]
        aistatus[ai-status :8097]
        aistatus --> rs
    end
    subgraph D [desktop - RTX 5080 16 GB]
        rd[llama.cpp router :8080<br/>one model at a time<br/>27B only in AI mode, 9B otherwise]
        pcstatus[pc-status :8097]
        aimode[ai-mode<br/>flock, pauses video wallpaper]
        cl[claude-local<br/>Claude Code -> local router]
        pcstatus --> rd
        pcstatus -->|user idle 10 min| aimode
        cl --> aimode
        cl --> rd
    end
    dash[Dashboard AI page] --> aistatus
    dash --> pcstatus
    jobs[Batch jobs<br/>job-offer scoring, summaries] --> rs
    jobs -.->|when the desktop is free| rd
```

- Both routers are `llama-server` in router mode (`--models-preset presets.ini`): one HTTP endpoint, models
  loaded and unloaded by name, OpenAI-compatible `/v1/chat/completions` and Anthropic-compatible `/v1/messages`.
- `ai-status` and `pc-status` are the same script (`ai/status.py`), configured by environment variables.
- `ai-mode` exists only on the desktop; on the server `/ai-mode` returns 404 and `ai_mode` is `null`.

## Storage

```mermaid
flowchart LR
    subgraph server
        ssd[(system SSD<br/>Docker root, NFS export)]
        data[(data disk<br/>service volumes, models)]
        notes[(notes vault)]
    end
    desktop[desktop /home] -->|NFSv4, async| ssd
    notes -->|bind mount, sync export| desktop
    notes <--> syncthing[Syncthing - Windows peer]
    notes <--> couchdb[CouchDB LiveSync - phone]
    notes --> gitea[git autocommit -> Gitea]
```

Apps that keep many small files or SQLite/flock state (Steam, Flatpak data, browser cache) live on the desktop's
local disk with a symlink, not on the NFS home.
