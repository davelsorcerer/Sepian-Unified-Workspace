#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
datetime_plugin.py - alias wrapper around TimePlugin.

Why this exists:
    Some callers (notably the voice pipeline) ask for a plugin named
    "DatetimePlugin" because they think in human-language terms like
    "what's the date and time". TimePlugin already does the work; this
    file just re-exports it under both names so either lookup succeeds.

Commands (delegated to TimePlugin):
    now        -> current wall-clock time
    parse      -> parse a timestamp string into ISO-8601 UTC
    diff       -> gap between two timestamps
    offset     -> convert a timestamp from one tz to another

No external deps; just stdlib.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from sepian_plugin import SepianPlugin
except Exception:
    SepianPlugin = object

from plugins.time_plugin import TimePlugin, DEFAULT_TZ


PLUGIN_NAME = "DatetimePlugin"


class DatetimePlugin(TimePlugin):
    """
    Thin subclass of TimePlugin. Inherits every command so callers that
    look up the plugin by either name ("TimePlugin" or "DatetimePlugin")
    get the exact same behaviour.
    """

    name = "DatetimePlugin"
    version = "0.1.0"

    def get_description(self) -> str:
        return ("Datetime and timezone utilities (alias of TimePlugin): "
                "current time, parse timestamps, compute durations, "
                "convert between timezones.")

    def get_commands(self) -> List[str]:
        # Same surface as TimePlugin.
        return super().get_commands()


# Allow standalone smoke test: `python -m plugins.datetime_plugin`
if __name__ == "__main__":
    p = DatetimePlugin()
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
