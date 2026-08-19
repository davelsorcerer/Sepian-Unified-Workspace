#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
time_plugin.py — gives Adam awareness of the clock.

Why this exists:
    Before this plugin, the only way Adam could know the current time
    was to shell out to `date` via ApprovedShellPlugin. That's slow,
    noisy in the approval UI, and useless for things like "remind me
    in 20 minutes" or "how long ago did X happen?"

Commands (via plugin.execute(command, args)):
    now        -> current wall-clock time in the given (or default) tz.
                  args: tz (IANA name, optional), fmt (strftime, optional)
    parse      -> parse a free-form timestamp string into a normalized
                  ISO-8601 UTC string.
                  args: s (input string), tz (assume this if naive), fmt
    diff       -> compute the gap between two timestamps.
                  args: a, b (strings), tz (assume this if naive),
                        fmt_a, fmt_b (optional explicit strftime)
    offset     -> convert a timestamp from one timezone to another.
                  args: s, from_tz, to_tz, fmt (optional)

Implements the SepianPlugin interface so it loads through the same
registry as the other plugins. No external deps — only stdlib
datetime + zoneinfo (py3.9+).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    # py3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover — only on very old pythons
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

try:
    from sepian_plugin import SepianPlugin
except Exception:
    # Allow standalone import for unit testing without the host package.
    SepianPlugin = object


PLUGIN_NAME = "TimePlugin"

# Reasonable default for this household. Overridable per-call.
DEFAULT_TZ = "America/New_York"

# Try a handful of common free-form shapes before falling back to
# fromisoformat. Keeping this list short + explicit is better than
# pretending we have a real NLP parser.
_COMMON_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y",
    "%b %d, %Y %H:%M",
    "%b %d, %Y",
    "%B %d, %Y %H:%M",
    "%B %d, %Y",
    "%H:%M:%S",
    "%H:%M",
]


# --------------------------------------------------------------------- #
# Helpers (module-level so they're easy to unit-test in isolation)
# --------------------------------------------------------------------- #

def _resolve_tz(name: Optional[str]) -> Tuple[Optional[Any], Optional[str]]:
    """Return (ZoneInfo, None) on success, or (None, error_string)."""
    if name is None or name == "":
        name = DEFAULT_TZ
    if ZoneInfo is None:
        return None, "zoneinfo not available (need Python 3.9+)"
    try:
        return ZoneInfo(name), None
    except ZoneInfoNotFoundError:
        return None, f"unknown timezone: {name!r}"


def _iso(dt: datetime) -> str:
    """ISO-8601 with offset, no microseconds."""
    return dt.replace(microsecond=0).isoformat()


def _parse_one(s: str, fmt: Optional[str], assume_tz: Any) -> Tuple[Optional[datetime], Optional[str]]:
    """Try to parse `s`. If `fmt` is given, only try that. Otherwise try
    the common format list, then fall back to `datetime.fromisoformat`.
    Returns (dt, err)."""
    s = (s or "").strip()
    if not s:
        return None, "empty timestamp string"

    formats = [fmt] if fmt else _COMMON_FORMATS
    for f in formats:
        try:
            dt = datetime.strptime(s, f)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=assume_tz)
            return dt, None
        except ValueError:
            continue

    # Fall back to fromisoformat — handles e.g. "2026-08-13T19:21:00-04:00".
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=assume_tz)
        return dt, None
    except ValueError:
        return None, f"could not parse timestamp: {s!r}"


def _humanize(seconds: float) -> str:
    """Turn a (signed) number of seconds into e.g. '2h 13m 4s ago' or 'in 3 days'."""
    if seconds == 0:
        return "0s"
    s = abs(seconds)
    days, rem = divmod(int(s), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if secs and not (days or hours):
        parts.append(f"{secs}s")
    if not parts:
        parts.append("0s")

    core = " ".join(parts)
    return f"in {core}" if seconds < 0 else f"{core} ago"


# --------------------------------------------------------------------- #
# Plugin
# --------------------------------------------------------------------- #

class TimePlugin(SepianPlugin if SepianPlugin is not object else object):
    """Time/duration utilities. Stateless, no I/O, no approval needed."""

    # ---- SepianPlugin surface ------------------------------------------- #

    def __init__(self):
        self.name = PLUGIN_NAME
        self.enabled = True
        self.config = {}
        self.status_callback = None

    def get_description(self) -> str:
        return ("Time and timezone utilities: current time, parse timestamps, "
                "compute durations, convert between timezones.")

    def get_commands(self) -> List[str]:
        return ["now", "parse", "diff", "offset"]

    def get_default_config(self) -> Dict[str, Any]:
        return {
            "default_tz": DEFAULT_TZ,
        }

    def on_config_update(self):
        # If the user puts a default_tz in config, treat that as the new default.
        global DEFAULT_TZ
        tz = self.config.get("default_tz")
        if isinstance(tz, str) and tz:
            DEFAULT_TZ = tz

    def handle_voice_command(self, text: str) -> Optional[str]:
        """Light-weight voice hook: catch 'what time is it' / 'what's the date'."""
        if not text:
            return None
        t = text.lower().strip()
        if not t:
            return None
        # Strip a trailing punctuation mark like "?"/".".
        t = t.rstrip("?!. ")
        if t in ("what time is it", "what's the time", "whats the time",
                 "what time is it right now", "what time is it now"):
            res = self.execute("now", {})
            if res.get("ok"):
                return res["human"]
            return None
        if t in ("what's the date", "whats the date", "what day is it",
                 "what's today's date", "whats todays date"):
            res = self.execute("now", {})
            if res.get("ok"):
                return res["weekday"]
            return None
        return None

    # ---- execute dispatches --------------------------------------------- #

    def execute(self, command: str, args: Dict[str, Any]) -> Dict[str, Any]:
        cmd = (command or "").strip().lower()
        if cmd == "now":
            return self._cmd_now(args)
        if cmd == "parse":
            return self._cmd_parse(args)
        if cmd == "diff":
            return self._cmd_diff(args)
        if cmd == "offset":
            return self._cmd_offset(args)
        return {"ok": False, "error": f"unknown command: {command!r}"}

    # ----- now ----- #
    def _cmd_now(self, args: Dict[str, Any]) -> Dict[str, Any]:
        tz, err = _resolve_tz(args.get("tz"))
        if err:
            return {"ok": False, "error": err}
        fmt = args.get("fmt")

        local = datetime.now(tz=tz)
        utc = datetime.now(tz=timezone.utc)

        out: Dict[str, Any] = {
            "ok": True,
            "tz": str(tz),
            "local": _iso(local),
            "utc": _iso(utc),
            "epoch": int(local.timestamp()),
            "weekday": local.strftime("%A"),
            "human": local.strftime("%A, %B %d, %Y at %I:%M:%S %p %Z"),
        }
        if fmt:
            try:
                out["formatted"] = local.strftime(fmt)
            except ValueError as e:
                return {"ok": False, "error": f"bad strftime fmt: {e}"}
        return out

    # ----- parse ----- #
    def _cmd_parse(self, args: Dict[str, Any]) -> Dict[str, Any]:
        s = args.get("s", "")
        fmt = args.get("fmt")
        tz, err = _resolve_tz(args.get("tz"))
        if err:
            return {"ok": False, "error": err}

        dt, perr = _parse_one(s, fmt, tz)
        if perr:
            return {"ok": False, "error": perr}

        return {
            "ok": True,
            "input": s,
            "tz": str(tz),
            "local": _iso(dt),
            "utc": _iso(dt.astimezone(timezone.utc)),
            "epoch": int(dt.timestamp()),
        }

    # ----- diff ----- #
    def _cmd_diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        a = args.get("a", "")
        b = args.get("b", "")
        assume_tz, err = _resolve_tz(args.get("tz"))
        if err:
            return {"ok": False, "error": err}

        da, err_a = _parse_one(a, args.get("fmt_a"), assume_tz)
        if err_a:
            return {"ok": False, "error": f"could not parse 'a': {err_a}"}
        db, err_b = _parse_one(b, args.get("fmt_b"), assume_tz)
        if err_b:
            return {"ok": False, "error": f"could not parse 'b': {err_b}"}

        delta = db - da
        seconds = delta.total_seconds()
        return {
            "ok": True,
            "a": _iso(da),
            "b": _iso(db),
            "seconds": seconds,
            "human": _humanize(seconds),
        }

    # ----- offset ----- #
    def _cmd_offset(self, args: Dict[str, Any]) -> Dict[str, Any]:
        s = args.get("s", "")
        from_tz, err = _resolve_tz(args.get("from_tz"))
        if err:
            return {"ok": False, "error": f"from_tz: {err}"}
        to_tz, err = _resolve_tz(args.get("to_tz"))
        if err:
            return {"ok": False, "error": f"to_tz: {err}"}

        dt, perr = _parse_one(s, args.get("fmt"), from_tz)
        if perr:
            return {"ok": False, "error": perr}

        out = dt.astimezone(to_tz)
        return {
            "ok": True,
            "input": s,
            "from_tz": str(from_tz),
            "to_tz": str(to_tz),
            "converted": _iso(out),
            "epoch": int(out.timestamp()),
        }


# Allow `python -m plugins.time_plugin` smoke test
if __name__ == "__main__":
    p = TimePlugin()
    print("Description:", p.get_description())
    print("Commands: ", p.get_commands())
    print("Default config:", p.get_default_config())
    print()
    print("now:", p.execute("now", {}))
    print()
    print("parse ISO:", p.execute("parse", {"s": "2026-08-13 19:21"}))
    print()
    print("diff:    ", p.execute("diff", {
        "a": "2026-08-13 10:00:00",
        "b": "2026-08-13 12:13:04",
    }))
    print()
    print("offset:  ", p.execute("offset", {
        "s": "2026-08-13 19:00",
        "from_tz": "America/New_York",
        "to_tz": "Europe/London",
    }))
