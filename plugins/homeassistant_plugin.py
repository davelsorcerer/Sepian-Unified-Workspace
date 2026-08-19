#!/usr/bin/env python3
"""
plugins/homeassistant_plugin.py - Home Assistant smart home plugin

Controls entities exposed by a Home Assistant instance via its REST API.
This plugin is generic over Home Assistant's entity model, so the same
plugin can drive lights, switches, scenes, scripts, input_booleans, etc.
without needing a per-device-type plugin.

Configuration (config/config.json):
  HomeAssistantPlugin:
    enabled: true
    config:
      base_url: "http://homeassistant.local:8123"  # HA instance URL
      token: "<long-lived access token>"           # HA auth token
      verify_tls: true                              # verify HTTPS certs
      default_domain: "light"                       # used by call_service when domain is omitted
      timeout: 10                                    # seconds for HTTP requests

Voice hooks intentionally return None by default to avoid hijacking
"turn on the lights" phrases; add explicit phrases inside
handle_voice_command if you want HA voice control.
"""
import os
import re
import requests
from sepian_plugin import SepianPlugin


def _expand_env(value):
    """Expand ${VAR} references from the process environment.

    Only strings are expanded; other types pass through unchanged so a
    missing var on a non-string field is a no-op. A literal '$$' becomes
    a single '$' (POSIX-shell-style escape) so the user can write a real
    dollar sign if they ever need to.

    Unresolved placeholders like ${SEPIAN_HA_TOKEN} are treated as empty so a
    missing env var does not become a bogus literal token value.
    """
    if isinstance(value, str):
        # '$$' -> '$' first, so subsequent expansion doesn't treat it as a var.
        expanded = value.replace("$$", "__SEPIAN_DOLLAR__")
        expanded = os.path.expandvars(expanded)
        expanded = expanded.replace("__SEPIAN_DOLLAR__", "$")
        expanded = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "", expanded)
        return expanded
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value

class HomeAssistantPlugin(SepianPlugin):

    def get_description(self) -> str:
        return "Home Assistant smart home control via REST API"

    def get_commands(self) -> list:
        return [
            "list_entities",
            "find_entity",
            "get_state",
            "turn_on",
            "turn_off",
            "toggle",
            "call_service",
            "list_services",
            "fire_event",
            "set_brightness",
            "set_color",
            "set_temp",
            "discover",
        ]

    def get_default_config(self) -> dict:
        return {
            "base_url": "",
            "token": "",
            "verify_tls": True,
            "default_domain": "light",
            "timeout": 10,
        }

    def on_config_update(self):
        # Expand ${VAR} from the environment so secrets like the HA token
        # can live in an env var / systemd unit / .env instead of the JSON.
        # NOTE: env-var expansion only happens here, not on every request, so
        # if you change an env var at runtime, hit 'Reload plugins' (or
        # restart Sepian) so on_config_update runs again.
        self.config = _expand_env(self.config)

        self.base_url = (self.config.get("base_url") or "").rstrip("/").strip()
        # .strip() so an env var that resolves to " " doesn't sneak through
        # as a non-empty token and trigger 401s.
        self.token = (self.config.get("token") or "").strip()
        if self.token.startswith("${") and "}" in self.token:
            self.token = ""
        if self.token.startswith("${") and "}" in self.token:
            self.token = ""
        self.verify_tls = bool(self.config.get("verify_tls", True))
        self.default_domain = (self.config.get("default_domain", "light") or "light").strip()
        try:
            self.timeout = float(self.config.get("timeout", 10))
        except (TypeError, ValueError):
            self.timeout = 10.0

    def _debug_config(self) -> dict:
        """Return a redacted view of the resolved config.

        Useful when 'HA isn't connecting' -- call this from the plugin host
        to see what on_config_update actually parsed, without leaking the
        token. Add to get_commands() if you want it exposed to users.
        """
        token = self.token or ""
        redacted = f"<len={len(token)}>{'*' * min(len(token), 4) if token else ''}"
        return {
            "base_url": self.base_url,
            "token": redacted,
            "verify_tls": self.verify_tls,
            "default_domain": self.default_domain,
            "timeout": self.timeout,
            "ready": bool(self.base_url and self.token),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ready(self):
        """Return (ok, error_or_none)."""
        if not self.base_url:
            return False, "Home Assistant base_url is not configured."
        if not self.token:
            return False, "Home Assistant token is not configured."
        return True, None

    def _headers(self):
        token = (self.token or "").strip()
        if token.lower().startswith("bearer ") or token.lower().startswith("token "):
            auth_value = token
        else:
            auth_value = "Bearer " + token
        return {
            "Authorization": auth_value,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, **kwargs):
        ok, err = self._ready()
        if not ok:
            return None, err
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify_tls)
        kwargs.setdefault("headers", self._headers())
        try:
            r = requests.request(method, self._url(path), **kwargs)
        except requests.RequestException as e:
            return None, f"HTTP error: {e}"
        return r, None

    def _ok(self, **extra):
        out = {"ok": True}
        out.update(extra)
        return out

    def _fail(self, error: str, **extra):
        out = {"ok": False, "error": error}
        out.update(extra)
        return out

    def _entity_id(self, args: dict) -> str:
        """Return the entity_id from args, accepting either 'entity_id' or 'entity'.

        If a model supplies a friendly name (for example "living room lamp")
        instead of a raw entity_id, try to resolve it against Home Assistant's
        known entities automatically.
        """
        for key in ("entity_id", "entity", "entity_name", "name", "friendly_name"):
            value = args.get(key)
            if value is None or value == "":
                continue
            candidate = str(value).strip()
            if not candidate:
                continue
            if "." in candidate:
                return candidate
            resolved = self._resolve_entity_name(candidate, args)
            if resolved:
                return resolved
        return ""

    def _resolve_entity_name(self, phrase: str, args: dict) -> str:
        """Best-effort friendly-name lookup for a human phrase."""
        phrase = (phrase or "").strip()
        if not phrase:
            return ""
        listing = self.execute("list_entities", {"domain": args.get("domain") or self.default_domain})
        if not listing.get("ok"):
            return ""
        phrase_lower = phrase.lower()
        for entity in listing.get("entities", []):
            friendly = (entity.get("friendly_name") or "").lower()
            entity_id = (entity.get("entity_id") or "").strip()
            if not entity_id:
                continue
            if phrase_lower == friendly or phrase_lower == entity_id.lower():
                return entity_id
            if phrase_lower in friendly or friendly in phrase_lower:
                return entity_id
        return ""

    @staticmethod
    def _parse_rgb_color(value):
        """Return (r, g, b) from a color name, hex string, or RGB list/tuple."""
        if isinstance(value, (list, tuple)) and len(value) == 3:
            try:
                return tuple(max(0, min(255, int(v))) for v in value)
            except (TypeError, ValueError):
                pass

        if isinstance(value, dict):
            try:
                return (
                    max(0, min(255, int(value.get("r", 255)))),
                    max(0, min(255, int(value.get("g", 255)))),
                    max(0, min(255, int(value.get("b", 255)))),
                )
            except (TypeError, ValueError):
                pass

        if isinstance(value, str):
            candidate = value.strip().lower()
            if not candidate:
                return None

            candidate = candidate.strip("[]()")
            if candidate.startswith("#"):
                candidate = candidate[1:]
            if len(candidate) == 6 and all(ch in "0123456789abcdef" for ch in candidate):
                try:
                    return tuple(int(candidate[i:i + 2], 16) for i in (0, 2, 4))
                except ValueError:
                    pass

            if "," in candidate:
                try:
                    parts = [int(p.strip().strip("[]()")) for p in candidate.split(",")]
                    if len(parts) == 3:
                        return tuple(max(0, min(255, p)) for p in parts)
                except ValueError:
                    pass

            named = {
                "red": (255, 0, 0),
                "green": (0, 255, 0),
                "blue": (0, 0, 255),
                "white": (255, 255, 255),
                "warm": (255, 180, 120),
                "cool": (180, 210, 255),
                "yellow": (255, 255, 0),
                "orange": (255, 128, 0),
                "magenta": (255, 0, 255),
                "purple": (128, 0, 255),
                "pink": (255, 105, 180),
                "cyan": (0, 255, 255),
                "teal": (0, 128, 128),
                "lavender": (180, 140, 255),
                "amber": (255, 191, 0),
                "crimson": (220, 20, 60),
                "navy": (0, 0, 128),
                "forest": (34, 139, 34),
                "salmon": (250, 128, 114),
                "ivory": (255, 255, 240),
                "dim": (80, 80, 80),
                "offwhite": (245, 245, 245),
                "gold": (255, 215, 0),
                "aqua": (0, 255, 255),
            }
            return named.get(candidate)

        return None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def execute(self, command: str, args: dict) -> dict:
        handler = getattr(self, f"_cmd_{command}", None)
        if handler is None:
            return self._fail(f"Unknown command: {command}")
        try:
            return handler(args) or self._fail("No result")
        except Exception as e:
            return self._fail(f"{type(e).__name__}: {e}")

    def _cmd_list_entities(self, args: dict) -> dict:
        r, err = self._request("GET", "/api/states")
        if err:
            return self._fail(err)
        if r.status_code != 200:
            return self._fail(f"HA returned HTTP {r.status_code}: {r.text[:200]}")
        try:
            states = r.json()
        except ValueError:
            return self._fail("HA returned non-JSON response")

        domain_filter = (args.get("domain") or "").strip().lower()
        search = (args.get("search") or "").strip().lower()

        results = []
        for s in states:
            eid = s.get("entity_id", "")
            if domain_filter and not eid.startswith(domain_filter + "."):
                continue
            if search and search not in eid.lower() \
                    and search not in (s.get("attributes", {}).get("friendly_name", "") or "").lower():
                continue
            results.append({
                "entity_id": eid,
                "state": s.get("state"),
                "friendly_name": s.get("attributes", {}).get("friendly_name"),
                "domain": eid.split(".", 1)[0] if "." in eid else "",
            })

        self.notify_status(f"HA: {len(results)} entities")
        return self._ok(count=len(results), entities=results)

    def _cmd_find_entity(self, args: dict) -> dict:
        query = (args.get("query") or args.get("name") or args.get("entity") or args.get("friendly_name") or "").strip()
        if not query:
            return self._fail("query is required for find_entity")
        listing = self.execute("list_entities", {"domain": args.get("domain") or self.default_domain})
        if not listing.get("ok"):
            return self._fail(listing.get("error", "Unable to list entities"))

        matches = []
        query_lower = query.lower()
        for entity in listing.get("entities", []):
            friendly = (entity.get("friendly_name") or "").lower()
            entity_id = entity.get("entity_id", "")
            state = entity.get("state")
            if not entity_id:
                continue
            score = 0
            if query_lower == friendly or query_lower == entity_id.lower():
                score = 100
            elif query_lower in friendly or friendly in query_lower:
                score = 80
            elif query_lower in entity_id.lower():
                score = 60
            if score:
                matches.append({
                    "entity_id": entity_id,
                    "friendly_name": entity.get("friendly_name"),
                    "state": state,
                    "score": score,
                })
        matches.sort(key=lambda item: (-item["score"], item["entity_id"]))
        return self._ok(count=len(matches), matches=matches[:10])

    def _cmd_get_state(self, args: dict) -> dict:
        eid = self._entity_id(args)
        if not eid:
            return self._fail("entity_id is required for get_state")
        r, err = self._request("GET", f"/api/states/{eid}")
        if err:
            return self._fail(err)
        if r.status_code == 404:
            return self._fail(f"Entity not found: {eid}")
        if r.status_code != 200:
            return self._fail(f"HA returned HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except ValueError:
            return self._fail("HA returned non-JSON response")
        return self._ok(
            entity_id=data.get("entity_id"),
            state=data.get("state"),
            attributes=data.get("attributes", {}),
            last_changed=data.get("last_changed"),
            last_updated=data.get("last_updated"),
        )

    def _domain_for(self, entity_id: str, args: dict) -> str:
        dom = (args.get("domain") or "").strip()
        if dom:
            return dom
        if "." in entity_id:
            return entity_id.split(".", 1)[0]
        return self.default_domain

    def _service_for_action(self, entity_id: str, action: str, args: dict) -> str:
        """Pick the right service for a high-level action.
        Falls back to default_domain (e.g. 'homeassistant.toggle' if HA has
        no domain-specific service for the requested action)."""
        domain = self._domain_for(entity_id, args)
        action = action.lower()
        if domain == "light":
            return {
                "turn_on": "light.turn_on",
                "turn_off": "light.turn_off",
                "toggle": "light.toggle",
            }.get(action, f"{domain}.{action}")
        if domain in ("switch", "input_boolean", "fan"):
            return {
                "turn_on": f"{domain}.turn_on",
                "turn_off": f"{domain}.turn_off",
                "toggle": f"{domain}.toggle",
            }.get(action, f"{domain}.{action}")
        # Generic fallback - works for any domain that exposes these services.
        return f"{domain}.{action}"

    def _do_service(self, service: str, args: dict) -> dict:
        eid = self._entity_id(args)
        if not eid:
            return self._fail("entity_id is required")
        if "." not in service:
            return self._fail(f"service must be in 'domain.service' form, got {service!r}")

        # Allow caller to override or omit entity_id in service_data.
        service_data = dict(args.get("service_data") or {})
        if "entity_id" not in service_data:
            service_data["entity_id"] = eid

        path = f"/api/services/{service.replace('.', '/', 1)}"
        r, err = self._request("POST", path, json=service_data)
        if err:
            return self._fail(err)
        if r.status_code not in (200, 201):
            return self._fail(f"HA returned HTTP {r.status_code}: {r.text[:200]}")
        self.notify_status(f"HA: {service} -> {eid}")
        return self._ok(entity_id=eid, service=service, service_data=service_data)

    def _cmd_turn_on(self, args: dict) -> dict:
        eid = self._entity_id(args)
        if not eid:
            return self._fail("entity_id is required for turn_on")
        return self._do_service(self._service_for_action(eid, "turn_on", args), args)

    def _cmd_turn_off(self, args: dict) -> dict:
        eid = self._entity_id(args)
        if not eid:
            return self._fail("entity_id is required for turn_off")
        return self._do_service(self._service_for_action(eid, "turn_off", args), args)

    def _cmd_toggle(self, args: dict) -> dict:
        eid = self._entity_id(args)
        if not eid:
            return self._fail("entity_id is required for toggle")
        return self._do_service(self._service_for_action(eid, "toggle", args), args)

    def _cmd_call_service(self, args: dict) -> dict:
        service = (args.get("service") or "").strip()
        if not service:
            return self._fail("service is required (e.g. 'light.turn_on')")
        return self._do_service(service, args)

    def _cmd_list_services(self, args: dict) -> dict:
        r, err = self._request("GET", "/api/services")
        if err:
            return self._fail(err)
        if r.status_code != 200:
            return self._fail(f"HA returned HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except ValueError:
            return self._fail("HA returned non-JSON response")
        services = []
        if isinstance(data, dict):
            items = data.items()
        else:
            items = data
        for domain, svcs in items:
            if isinstance(svcs, dict):
                names = list(svcs.keys())
            else:
                names = svcs
            for svc in names:
                services.append({"domain": domain, "service": svc})
        self.notify_status(f"HA: {len(services)} services")
        return self._ok(count=len(services), services=services)

    def _cmd_fire_event(self, args: dict) -> dict:
        event_type = (args.get("event_type") or args.get("event") or "").strip()
        if not event_type:
            return self._fail("event_type is required for fire_event")
        event_data = args.get("event_data") or args.get("data") or {}
        r, err = self._request("POST", "/api/events/" + event_type, json=event_data)
        if err:
            return self._fail(err)
        if r.status_code not in (200, 201, 204):
            return self._fail(f"HA returned HTTP {r.status_code}: {r.text[:200]}")
        self.notify_status(f"HA: event {event_type} fired")
        return self._ok(event_type=event_type, event_data=event_data)

    # ---- Light-specific conveniences ---------------------------------

    def _ensure_light(self, args: dict):
        eid = self._entity_id(args)
        if not eid:
            return None, self._fail("entity_id is required")
        if self._domain_for(eid, args) != "light":
            return None, self._fail(
                f"This command is for light entities; {eid!r} is not a light")
        return eid, None

    def _cmd_set_brightness(self, args: dict) -> dict:
        eid, err = self._ensure_light(args)
        if err:
            return err
        pct = args.get("percent")
        try:
            pct = max(0, min(100, int(pct)))
        except (TypeError, ValueError):
            return self._fail("percent must be an integer 0..100")
        bri = int(round((pct / 100.0) * 255))
        args["service_data"] = {"brightness": bri}
        result = self._do_service("light.turn_on", args)
        if result.get("ok"):
            self.notify_status(f"HA: {eid} brightness {pct}%")
        return result

    def _cmd_set_color(self, args: dict) -> dict:
        eid, err = self._ensure_light(args)
        if err:
            return err

        color_value = args.get("color")
        if color_value is None:
            color_value = args.get("name")
        if color_value is None:
            color_value = args.get("color_name")
        if color_value is None:
            color_value = args.get("rgb_value")
        if color_value is None:
            color_value = args.get("rgb")
        if color_value is None:
            color_value = args.get("rgb_color")

        if color_value is not None:
            parsed = self._parse_rgb_color(color_value)
            if parsed is None:
                return self._fail("color must be an RGB tuple/list, a literal string like '[255, 0, 0]', a hex '#RRGGBB', or a named color like red, blue, orange, magenta")
            r, g, b = parsed
        else:
            try:
                r = max(0, min(255, int(args.get("r", 255))))
                g = max(0, min(255, int(args.get("g", 255))))
                b = max(0, min(255, int(args.get("b", 255))))
            except (TypeError, ValueError):
                return self._fail("r/g/b must be integers 0..255 or pass a named/hex/rgb_value color")

        args["service_data"] = {"rgb_color": [r, g, b]}
        result = self._do_service("light.turn_on", args)
        if result.get("ok"):
            self.notify_status(f"HA: {eid} color ({r},{g},{b})")
        return result

    def _cmd_set_temp(self, args: dict) -> dict:
        eid, err = self._ensure_light(args)
        if err:
            return err
        try:
            kelvin = int(args.get("kelvin") or args.get("temp"))
        except (TypeError, ValueError):
            return self._fail("kelvin must be an integer")
        if not (1000 <= kelvin <= 40000):
            return self._fail("kelvin must be between 1000 and 40000")
        args["service_data"] = {"color_temp_kelvin": kelvin}
        result = self._do_service("light.turn_on", args)
        if result.get("ok"):
            self.notify_status(f"HA: {eid} temp {kelvin}K")
        return result

    # ---- Discovery (best-effort LAN hint) ----------------------------

    def _cmd_discover(self, args: dict) -> dict:
        # HA doesn't have a real discovery protocol. We just probe the
        # configured base_url; if unset, we report that.
        ok, err = self._ready()
        if not ok:
            return self._fail(err)
        r, err = self._request("GET", "/api/")
        if err:
            return self._fail(err)
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                data = {}
            return self._ok(base_url=self.base_url, ha=data)
        return self._fail(
            f"Home Assistant not reachable at {self.base_url} "
            f"(HTTP {r.status_code})")

    # ------------------------------------------------------------------
    # Voice hook (intentionally conservative)
    # ------------------------------------------------------------------

    def handle_voice_command(self, text: str) -> str:
        t = (text or "").lower()
        if "home assistant" not in t and "ha " not in t:
            return None  # never hijack non-HA phrases

        # "home assistant list lights" -> list_entities
        if "list" in t and ("lights" in t or "entities" in t):
            result = self.execute("list_entities", {"domain": "light"})
            if result.get("ok"):
                return f"Home Assistant has {result.get('count', 0)} light entities"
            return f"Error: {result.get('error')}"

        # "home assistant turn off <entity-ish phrase>"
        m = re.search(r"home assistant\s+(turn\s+(on|off)|toggle)\s+(.+)", t)
        if m:
            verb, target = m.group(1), m.group(2).strip()
            eid = self._guess_entity_id(target)
            if not eid:
                return f"Couldn't map {target!r} to an entity_id"
            cmd = "turn_on" if "on" in verb else ("turn_off" if "off" in verb else "toggle")
            result = self.execute(cmd, {"entity_id": eid})
            return ("OK" if result.get("ok") else f"Error: {result.get('error')}")

        return None

    def _guess_entity_id(self, phrase: str) -> str:
        """Map a freeform phrase to a likely entity_id by friendly_name."""
        # Reuse list_entities to do the matching.
        listing = self.execute("list_entities", {})
        if not listing.get("ok"):
            return ""
        phrase = (phrase or "").lower().strip()
        if not phrase:
            return ""
        # Exact friendly_name match first.
        for e in listing.get("entities", []):
            name = (e.get("friendly_name") or "").lower()
            if name == phrase:
                return e.get("entity_id", "")
        # Substring match.
        for e in listing.get("entities", []):
            name = (e.get("friendly_name") or "").lower()
            if phrase in name or name in phrase:
                return e.get("entity_id", "")
        return ""
