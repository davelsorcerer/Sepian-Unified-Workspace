# ha — Sepian-friendly Home Assistant CLI

A thin wrapper around Home Assistant's REST API for use from the Sepian chat.

## Why this exists

The Home Assistant plugin lives inside the Sepian server and works fine, but the Sepian chat assistant has no general "invoke any plugin" tool. This script bridges that gap by talking to HA directly via `curl`, so the chat can say things like "turn off light.kitchen" and have it actually happen.

## Setup

1. **Token** — set one of:
   - `export HA_TOKEN=...` (cleanest; survives restarts if you put it in `.bashrc` or a systemd unit)
   - Or have HA token in `config/config.json` under `HomeAssistantPlugin.config.token` (no extra setup; the script falls back to it automatically)

2. **URL** — default is `http://10.0.0.73`. Override with `export HA_URL=http://your-ha:port`.

3. **Make executable** — `chmod +x bin/ha` (a future test will fail loudly if this is missing).

## Commands

```
ha status                              show URL + redacted token (safe to share)
ha list [--domain X] [--search S]      list entities (default first 50)
ha get <entity_id>                     full state + attributes for one entity
ha on <entity_id>                      turn on
ha off <entity_id>                     turn off
ha toggle <entity_id>                  flip current state
ha scene {activate|on} <scene.entity>  activate a HA scene
ha brightness <entity_id> <0-255|0-100%>
ha color <entity_id> <name|hex|R,G,B>  names: red,green,blue,warm,cool,teal,...
ha temp <entity_id> <mireds|warm|cool|daylight>
ha help                                this message
```

## Examples

```bash
ha status
ha list --domain light
ha list --search kitchen
ha get light.kitchen_main
ha off light.light_1
ha toggle light.light_1                    # flip on<->off
ha scene activate scene.movie_night        # or: ha scene on scene.movie_night
ha brightness light.kitchen_main 50%       # 0-100% or 0-255
ha color light.kitchen_main blue           # or "#00ff00" or "0,0,255"
ha color light.kitchen_main teal
ha temp light.kitchen_main warm            # or "370" (raw mireds)
```

## Exit codes

- `0` — success
- `2` — user error (bad args, missing token, unknown color)
- `3` — HA / network error (HA's error message is on stderr)

## Supported color names

red, green, blue, white, warm, cool, yellow, orange, purple, pink, magenta, cyan, teal, lavender, amber, crimson, navy, forest, salmon, ivory, dim

Also accepts `#RRGGBB` hex and `R,G,B` triplets directly.

## Color temperature presets

- `warm` / `incandescent` — 370 mireds
- `soft` — 300 mireds
- `cool` — 180 mireds
- `daylight` / `bright` — 250 mireds
- Or raw mireds (150-500) directly

## Notes / non-goals

- Read-only-ish: `on`, `off`, `brightness`, `color`, `temp` are the only state-changing commands. No arbitrary service calls (deliberate — keeps the surface small and the chat less likely to break your HA).
- No persistence: each call is a fresh curl. That's fine for HA, but if you need to script a "morning routine" that turns on 5 lights with a delay, do it in HA itself, not here.
- No TLS verification toggle yet. HA on `http://10.0.0.73:80` is plain HTTP, so it doesn't matter. If you ever move to HTTPS with a self-signed cert, edit `CURL_OPTS` to add `--insecure` (or `-k`).
