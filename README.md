# Sepian

A Python-based AI / smart-home assistant with a Tkinter GUI, voice input and
output, an animated avatar (viseme-based mouth shapes), and a plugin system
covering Home Assistant, Fire TV control, person detection, an approval-gated
shell, and several side utilities (tic-tac-toe, time, section reader, etc.).

## Features

- Turn lights on/off; adjust brightness, color, and temperature (WiZ and Hue).
- Control a Fire TV Stick: power, volume, menu navigation, launch apps
  (Netflix, YouTube, Prime Video, Disney+, Hulu, Plex).
- Run approved shell commands via the **Approved Shell Plugin** — every
  command requires manual approval.
- Manage sticky approvals and review approval history.
- Person detection via OpenCV, used to trigger a greeting and head-tracking
  behaviour.
- Tictactoe, YouTube launcher, map and property views, and other side plugins.

## Project layout

```
Sepian-Unified-Workspace/
├── sepianai.py              # main application entry point
├── plugin_manager.py        # plugin loader / lifecycle
├── sepian_plugin.py         # base plugin class
├── requirements.txt
├── sepian_about.txt
├── AI.png, default.png, background.png   # avatar assets
├── E.png, F.png, K.png, L.png, M.png,    # viseme / mouth-shape images
│   O.png, S.png, SH.png, TH.png, U.png,
│   middlelook.png, lookingleft.png, lookingright.png
│                                      # transparent eye-position layers
│   Eyes-closed.png
└── plugins/
    ├── __init__.py
    ├── approved_shell_plugin.py
    ├── datetime_plugin.py
    ├── firetv_plugin.py
    ├── fullscreen_head_plugin.py
    ├── homeassistant_plugin.py
    ├── manager_plugin.py
    ├── map_plugin.py
    ├── person_detector.py
    ├── section_reader_plugin.py
    ├── self_dev_plugin.py
    ├── tictactoe_plugin.py
    ├── time_plugin.py
    ├── plugin_config.example.json
    ├── plugin_config.json          # gitignored, see Security
    ├── zones.json
    ├── property_map.jpg
    └── haarcascade_frontalface_default(1).xml
```

## Requirements

- Python 3.13 (the project's `__pycache__/` indicates 3.13 is the active
  interpreter; older 3.10 caches were removed during cleanup).
- Tkinter (system package: `sudo apt install python3-tk` on Debian/Ubuntu).
- See `requirements.txt` for Python dependencies. Highlights:
  - `SpeechRecognition`, `PyAudio` — voice input
  - `edge-tts`, `pygame` — text-to-speech playback
  - `Pillow` — avatar image handling
  - `zeroconf` — local network discovery for smart-home devices
  - `openai-whisper` *(optional)* — offline speech recognition
- `adb` (system) is required for Fire TV control.

## Install

```bash
# Linux system deps (Debian/Ubuntu). The bundled ./install script does this
# for you automatically — run that instead if you prefer.
sudo apt install python3-tk python3-dev build-essential \
                 adb portaudio19-dev libasound2-dev libbluetooth-dev

# Python deps (the ./install script handles this in a venv too).
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> Tip: just run `./install` from this directory — it handles both system
> packages (with sudo) and Python deps in a venv, then installs a desktop
> launcher. Re-run it any time; it is idempotent.
>
> To uninstall, run `./uninstall`.

## Run

```bash
python3 sepianai.py
```

The main application initialises ALSA, loads `plugin_manager.py`, and brings
up the Tkinter window. A running instance writes detector events to
`plugins/detector.log` and presence state to `sepian_person_signal` in the
workspace root.

## Configuration

- `plugins/plugin_config.json` — plugin enable/disable and per-plugin options.
- `plugins/zones.json` — zones used by the map / property plugin.

## Security and secrets

`plugins/plugin_config.json` contains credentials for Home Assistant (and any
other plugin that talks to a network service). It is **gitignored** and must
never be committed. The committed template is `plugin_config.example.json`;
copy it and fill in your own values:

```bash
cp plugins/plugin_config.example.json plugins/plugin_config.json
# then edit plugins/plugin_config.json and replace PASTE_YOUR_RAW_HA_TOKEN_HERE
# with your real long-lived access token from your HA profile page
```

Rules of thumb:

- Never commit `plugins/plugin_config.json`, your real HA token, or any
  other live credentials. The `.gitignore` already excludes it; check
  with `git status` before every commit.
- If a token is ever leaked into the repo, treat it as compromised:
  revoke it in Home Assistant (Profile → Long-Lived Access Tokens →
  Delete) and create a new one. Then rotate any other secrets that shared
  the commit.
- `plugins/plugin_config.local.json` is also gitignored as a backup of the
  live config; same rules apply.
- The `detector.log` file in `plugins/` is a runtime log written by the
  person detector; safe to delete, and ignored by git.

## License

Released under the MIT License. See [`LICENSE`](LICENSE) for the full text.

## Home Assistant smart-home usage

Sepian can control Home Assistant entities through the `HomeAssistantPlugin`.
Use this when the model needs to turn lights on/off, toggle switches, call
scenes, or adjust climate/fan settings.

Recommended config:

```json
"HomeAssistantPlugin": {
  "enabled": true,
  "config": {
    "base_url": "http://homeassistant.local",
    "token": "PASTE_YOUR_RAW_HA_TOKEN_HERE",
    "verify_tls": false,
    "default_domain": "light",
    "timeout": 10
  }
}
```

Important:
- Keep the value in `token` as the raw Home Assistant long-lived access token.
- Do not include `Bearer ` in the config.
- The plugin adds the authorization header automatically.
- Use the real Home Assistant base URL reachable from this machine.
  For example `http://homeassistant.local` or `http://10.0.0.73`.

Common commands the model can use:

- `turn_on` — turn on a light or switch
- `turn_off` — turn off a light or switch
- `toggle` — flip a switch or light state
- `set_brightness` — dim a light by percent
- `set_color` — set an RGB color, hex color, or named color like blue, orange, magenta
- `set_temp` — set a light color temperature
- `call_service` — call an arbitrary HA service like `scene.turn_on`
- `find_entity` — resolve a friendly name like "living room light" to an entity_id

Example tool-call patterns for the model:

```json
{"tool": "HomeAssistantPlugin", "command": "turn_off", "args": {"entity_id": "light.light_2"}}
{"tool": "HomeAssistantPlugin", "command": "turn_on", "args": {"entity_id": "light.kitchen"}}
{"tool": "HomeAssistantPlugin", "command": "set_brightness", "args": {"entity_id": "light.living_room", "percent": 40}}
{"tool": "HomeAssistantPlugin", "command": "set_color", "args": {"entity_id": "light.living_room", "color": "magenta"}}
{"tool": "HomeAssistantPlugin", "command": "set_color", "args": {"entity_id": "light.living_room", "color": "orange"}}
{"tool": "HomeAssistantPlugin", "command": "find_entity", "args": {"query": "living room", "domain": "light"}}
```

If the user says a room name instead of an entity ID, the model should first
call `find_entity` and then use the returned `entity_id`.

## Notes

- **Person detector signal file** is created in the workspace root as
  `sepian_person_signal`. It is regenerated each time a person is detected
  or leaves the frame. While a person is present, it includes a `left`,
  `center`, or `right` gaze value used by the avatar. If `lookingright.png`
  is absent, Sepian mirrors `lookingleft.png` automatically.
