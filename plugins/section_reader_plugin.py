#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
section_reader_plugin.py - read large files in sections / pages.

Why this exists:
    Adam's built-in `read_file` truncates output for big files
    (e.g. sepianai.py is ~5,200 lines / ~4.5 MB and gets clipped to
    head+tail only). That makes it painful to inspect arbitrary
    regions of code, search for definitions, or page through a log.

    This plugin is a focused, dependency-free section reader that:
      * knows about ALLOWED_PATHS (mirrors SelfDevPlugin's rules so we
        never accidentally read outside the workspace), and
      * exposes a small, well-shaped command set the LLM can call:

Commands (via plugin.execute(command, args)):
    read_section   args: path, start_line, end_line, max_chars
    read_page      args: path, page (0-indexed), page_size, max_chars
    head           args: path, n (lines, default 50)
    tail           args: path, n (lines, default 50)
    count_lines    args: path
    grep           args: path, pattern, ignore_case, context (lines)
    list_files     args: path (dir), pattern (glob, optional), recursive
    stat           args: path   -> size, mtime, line_count, sha1 (short)

All commands return {'ok': True, ...} on success or
{'ok': False, 'error': '...'} on failure (matching the SepianPlugin
contract used elsewhere).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from sepian_plugin import SepianPlugin
except Exception:
    SepianPlugin = object


PLUGIN_NAME = "SectionReaderPlugin"

# Same allow-list policy as SelfDevPlugin. Kept in sync by hand:
#   - /home/davel/Sepian-Unified-Workspace
# If that ever changes, update both spots.
ALLOWED_PATHS = ("/home/davel/Sepian-Unified-Workspace",)

# Soft cap on bytes returned per call, to keep LLM context windows
# from getting nuked by accident. Reads beyond this return a
# fingerprint + instructions to page.
_DEFAULT_MAX_CHARS = 20_000
_ABSOLUTE_MAX_CHARS = 200_000


# --------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------- #

def _is_allowed(path: str) -> Tuple[bool, str]:
    """Return (ok, absolute_path_or_error)."""
    if not path:
        return False, "path is required"
    # Resolve both the input and the allowed roots to canonical
    # absolute paths so "../" or symlinks can't escape the sandbox.
    try:
        abs_path = os.path.realpath(os.path.abspath(path))
    except Exception as e:
        return False, f"could not resolve path: {e}"

    for root in ALLOWED_PATHS:
        try:
            abs_root = os.path.realpath(os.path.abspath(root))
        except Exception:
            continue
        # Use commonpath so '/foo/barbaz' doesn't match '/foo/bar'.
        try:
            common = os.path.commonpath([abs_path, abs_root])
        except ValueError:
            continue
        if common == abs_root:
            return True, abs_path

    return False, (
        f"path '{path}' is outside ALLOWED_PATHS "
        f"({', '.join(ALLOWED_PATHS)})"
    )


def _resolve_under(root: str, rel: str) -> str:
    """Treat bare relative paths as children of `root`."""
    if os.path.isabs(rel) or rel.startswith("/"):
        return rel
    return os.path.join(root, rel)


# --------------------------------------------------------------------- #
# Reading helpers
# --------------------------------------------------------------------- #

def _read_lines(path: str, start: int, end: int) -> Tuple[List[str], int]:
    """Read lines [start, end) (0-indexed, end exclusive).

    Returns (lines, total_line_count). Opens the file lazily and
    skips to `start` with a generator so we never materialize lines
    we don't need.
    """
    # Count total lines once (cheap-ish; same pass used to slice).
    # For very large files, we could swap to mmap / line index, but
    # we cap with _ABSOLUTE_MAX_CHARS downstream so simple is fine.
    total = 0
    with open(path, "rb") as f:
        # Bounded read: if file is huge, we'd rather fail fast than
        # drag the whole thing into memory.
        collected: List[str] = []
        line_iter = f  # type: ignore[assignment]
        for i, raw in enumerate(line_iter):
            total += 1
            if end is not None and i >= end:
                # We've read past the window we want; keep counting
                # only if we haven't already passed `end`. Cheap exit.
                if i >= end and len(collected) >= (end - start):
                    # Still need total count, so keep going but don't store.
                    pass
            if start <= i < (end if end is not None else total + 1):
                try:
                    collected.append(raw.decode("utf-8", errors="replace"))
                except Exception:
                    collected.append("\n")
        return collected, total


def _fingerprint(text: str) -> str:
    """Short, stable hash of a chunk so callers can verify alignment."""
    h = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
    return h[:10]


def _truncate(s: str, max_chars: int) -> Tuple[str, bool]:
    if len(s) <= max_chars:
        return s, False
    return s[:max_chars] + (
        f"\n\n... [truncated by SectionReaderPlugin at "
        f"{max_chars} chars] ..."
    ), True


# --------------------------------------------------------------------- #
# Plugin
# --------------------------------------------------------------------- #

class SectionReaderPlugin(SepianPlugin):
    """Sectioned file reader scoped to ALLOWED_PATHS."""

    def __init__(self):
        # Defensive init: works whether or not SepianPlugin was
        # actually importable (smoke test runs the file as a
        # standalone script and falls back to `object` as the base).
        try:
            super().__init__()
        except Exception:
            pass
        if not hasattr(self, "name"):
            self.name = self.__class__.__name__
        if not hasattr(self, "enabled"):
            self.enabled = True
        if not hasattr(self, "config") or self.config is None:
            self.config = {}
        if not hasattr(self, "status_callback"):
            self.status_callback = None
        # Apply our own defaults on top so get_default_config()
        # values survive even if the host later calls
        # set_config({}) and wipes them.
        for k, v in self.get_default_config().items():
            self.config.setdefault(k, v)

    def _max_chars(self, override: Optional[int]) -> int:
        """Resolve a safe max_chars value with full fallbacks."""
        cfg = getattr(self, "config", None) or {}
        if override is None or override == "":
            val = cfg.get("max_chars", _DEFAULT_MAX_CHARS)
        else:
            val = override
        try:
            val = int(val)
        except (TypeError, ValueError):
            val = _DEFAULT_MAX_CHARS
        return min(max(val, 100), _ABSOLUTE_MAX_CHARS)

    def get_description(self) -> str:
        return (
            "Read large files in sections / pages. Restricted to "
            f"ALLOWED_PATHS = {list(ALLOWED_PATHS)}."
        )

    def get_commands(self) -> List[str]:
        return [
            "read_section",
            "read_page",
            "head",
            "tail",
            "count_lines",
            "grep",
            "list_files",
            "stat",
        ]

    def get_default_config(self) -> Dict[str, Any]:
        return {
            "max_chars": _DEFAULT_MAX_CHARS,
        }

    # ----------------------- dispatch ----------------------- #
    def execute(self, command: str, args: Dict[str, Any]) -> Dict[str, Any]:
        handler = {
            "read_section": self._cmd_read_section,
            "read_page":    self._cmd_read_page,
            "head":         self._cmd_head,
            "tail":         self._cmd_tail,
            "count_lines":  self._cmd_count_lines,
            "grep":         self._cmd_grep,
            "list_files":   self._cmd_list_files,
            "stat":         self._cmd_stat,
        }.get(command)
        if handler is None:
            return {"ok": False, "error": f"unknown command: {command}"}
        try:
            return handler(args or {})
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ----------------------- read_section ----------------------- #
    def _cmd_read_section(self, args: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path", "")
        start = int(args.get("start_line", 0) or 0)
        end = args.get("end_line")
        end = int(end) if end not in (None, "", 0) else None
        max_chars = self._max_chars(args.get("max_chars"))

        ok, target = _is_allowed(_resolve_under(ALLOWED_PATHS[0], path))
        if not ok:
            return {"ok": False, "error": target}
        if not os.path.isfile(target):
            return {"ok": False, "error": f"not a file: {target}"}

        if start < 0:
            return {"ok": False, "error": "start_line must be >= 0"}
        if end is not None and end <= start:
            return {"ok": False, "error": "end_line must be > start_line"}

        lines, total = _read_lines(target, start, end)
        body = "".join(lines)
        truncated_body, was_truncated = _truncate(body, max_chars)

        return {
            "ok": True,
            "path": target,
            "start_line": start,
            "end_line": end if end is not None else total,
            "actual_end": start + len(lines),
            "total_lines": total,
            "lines_returned": len(lines),
            "fingerprint": _fingerprint(body),
            "truncated": was_truncated,
            "next_page_hint": (
                f"read_section(path='{target}', "
                f"start_line={start + len(lines)}, "
                f"end_line={start + len(lines) + 200})"
                if was_truncated else None
            ),
            "content": truncated_body,
        }

    # ----------------------- read_page ----------------------- #
    def _cmd_read_page(self, args: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path", "")
        page = max(int(args.get("page", 0) or 0), 0)
        page_size = int(args.get("page_size", 200) or 200)
        if page_size <= 0 or page_size > 5000:
            return {
                "ok": False,
                "error": "page_size must be between 1 and 5000",
            }
        max_chars = self._max_chars(args.get("max_chars"))

        start = page * page_size
        end = start + page_size
        res = self._cmd_read_section({
            "path": path, "start_line": start, "end_line": end,
            "max_chars": max_chars,
        })
        if res.get("ok"):
            res["page"] = page
            res["page_size"] = page_size
            res["total_pages"] = (
                (res["total_lines"] + page_size - 1) // page_size
                if res.get("total_lines") else None
            )
            res["next_page"] = page + 1 if res.get("truncated") else None
        return res

    # ----------------------- head ----------------------- #
    def _cmd_head(self, args: Dict[str, Any]) -> Dict[str, Any]:
        n = int(args.get("n", 50) or 50)
        if n <= 0 or n > 5000:
            return {"ok": False, "error": "n must be between 1 and 5000"}
        return self._cmd_read_section({
            "path": args.get("path", ""),
            "start_line": 0,
            "end_line": n,
            "max_chars": args.get("max_chars"),
        })

    # ----------------------- tail ----------------------- #
    def _cmd_tail(self, args: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path", "")
        n = int(args.get("n", 50) or 50)
        if n <= 0 or n > 5000:
            return {"ok": False, "error": "n must be between 1 and 5000"}

        ok, target = _is_allowed(_resolve_under(ALLOWED_PATHS[0], path))
        if not ok:
            return {"ok": False, "error": target}
        if not os.path.isfile(target):
            return {"ok": False, "error": f"not a file: {target}"}

        # Cheap tail: count lines, then read the last `n`.
        line_count = self._line_count(target)
        start = max(0, line_count - n)
        return self._cmd_read_section({
            "path": target,
            "start_line": start,
            "end_line": line_count,
            "max_chars": args.get("max_chars"),
        })

    # ----------------------- count_lines ----------------------- #
    def _cmd_count_lines(self, args: Dict[str, Any]) -> Dict[str, Any]:
        ok, target = _is_allowed(_resolve_under(ALLOWED_PATHS[0], args.get("path", "")))
        if not ok:
            return {"ok": False, "error": target}
        if not os.path.isfile(target):
            return {"ok": False, "error": f"not a file: {target}"}
        return {
            "ok": True, "path": target,
            "line_count": self._line_count(target),
        }

    @staticmethod
    def _line_count(path: str) -> int:
        n = 0
        with open(path, "rb") as f:
            for _ in f:
                n += 1
        return n

    # ----------------------- grep ----------------------- #
    def _cmd_grep(self, args: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path", "")
        pattern = args.get("pattern", "")
        if not pattern:
            return {"ok": False, "error": "pattern is required"}
        ignore_case = bool(args.get("ignore_case", True))
        context = max(int(args.get("context", 0) or 0), 0)
        max_chars = self._max_chars(args.get("max_chars"))
        limit_hits = int(args.get("limit", 200) or 200)

        ok, target = _is_allowed(_resolve_under(ALLOWED_PATHS[0], path))
        if not ok:
            return {"ok": False, "error": target}
        if not os.path.isfile(target):
            return {"ok": False, "error": f"not a file: {target}"}

        try:
            flags = re.IGNORECASE if ignore_case else 0
            rx = re.compile(pattern, flags)
        except re.error as e:
            return {"ok": False, "error": f"invalid regex: {e}"}

        hits: List[Dict[str, Any]] = []
        total_matches = 0
        prev_block: List[Tuple[int, str]] = []
        truncated = False

        with open(target, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        for idx, line in enumerate(lines):
            if rx.search(line):
                total_matches += 1
                if len(hits) < limit_hits:
                    block: List[Tuple[int, str]] = []
                    lo = max(0, idx - context)
                    hi = min(len(lines), idx + context + 1)
                    for j in range(lo, hi):
                        block.append((j, lines[j].rstrip("\n")))
                    hits.append({
                        "line": idx,
                        "text": line.rstrip("\n"),
                        "context": block if context > 0 else None,
                    })
            else:
                continue
            if len(hits) >= limit_hits and total_matches > limit_hits:
                truncated = True
                break

        rendered = self._render_grep(hits)
        rendered, was_truncated = _truncate(rendered, max_chars)

        return {
            "ok": True,
            "path": target,
            "pattern": pattern,
            "ignore_case": ignore_case,
            "total_matches": total_matches,
            "returned": len(hits),
            "limit": limit_hits,
            "truncated": was_truncated or truncated,
            "content": rendered,
        }

    @staticmethod
    def _render_grep(hits: List[Dict[str, Any]]) -> str:
        out: List[str] = []
        for h in hits:
            tag = f"{h['line']+1:>6}:{h['text']}"
            out.append(tag)
            if h.get("context"):
                for li, ltxt in h["context"]:
                    if li == h["line"]:
                        continue
                    out.append(f"       {li+1:>6}:{ltxt}")
                out.append("--")
        return "\n".join(out)

    # ----------------------- list_files ----------------------- #
    def _cmd_list_files(self, args: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path") or ALLOWED_PATHS[0]
        pattern = args.get("pattern") or "*"
        recursive = bool(args.get("recursive", False))
        limit = int(args.get("limit", 500) or 500)

        ok, target = _is_allowed(path)
        if not ok:
            return {"ok": False, "error": target}
        if not os.path.isdir(target):
            return {"ok": False, "error": f"not a directory: {target}"}

        import fnmatch
        results: List[Dict[str, Any]] = []
        try:
            if recursive:
                for root, _dirs, files in os.walk(target):
                    for fn in files:
                        if fnmatch.fnmatch(fn, pattern):
                            full = os.path.join(root, fn)
                            results.append(self._file_entry(full))
                            if len(results) >= limit:
                                break
                    if len(results) >= limit:
                        break
            else:
                for fn in sorted(os.listdir(target)):
                    if fnmatch.fnmatch(fn, pattern):
                        full = os.path.join(target, fn)
                        results.append(self._file_entry(full))
                        if len(results) >= limit:
                            break
        except Exception as e:
            return {"ok": False, "error": f"list failed: {e}"}

        return {
            "ok": True,
            "path": target,
            "pattern": pattern,
            "recursive": recursive,
            "count": len(results),
            "limit": limit,
            "files": results,
        }

    @staticmethod
    def _file_entry(full: str) -> Dict[str, Any]:
        try:
            st = os.stat(full)
            return {
                "path": full,
                "name": os.path.basename(full),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "is_dir": os.path.isdir(full),
            }
        except Exception:
            return {
                "path": full, "name": os.path.basename(full),
                "size": None, "mtime": None, "is_dir": False,
            }

    # ----------------------- stat ----------------------- #
    def _cmd_stat(self, args: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path", "")
        ok, target = _is_allowed(_resolve_under(ALLOWED_PATHS[0], path))
        if not ok:
            return {"ok": False, "error": target}
        if not os.path.exists(target):
            return {"ok": False, "error": f"not found: {target}"}

        st = os.stat(target)
        is_file = os.path.isfile(target)
        entry: Dict[str, Any] = {
            "ok": True,
            "path": target,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "ctime": int(st.st_ctime),
            "is_file": is_file,
            "is_dir": os.path.isdir(target),
        }
        if is_file:
            entry["line_count"] = self._line_count(target)
            try:
                with open(target, "rb") as f:
                    head = f.read(8192)
                entry["sha1_short"] = hashlib.sha1(head).hexdigest()[:10]
            except Exception:
                pass
        return entry


# --------------------------------------------------------------------- #
# Smoke test
# --------------------------------------------------------------------- #
if __name__ == "__main__":
    p = SectionReaderPlugin()
    print("Description:", p.get_description())
    print("Commands:   ", p.get_commands())
    print()
    print("stat sepianai.py ->", p.execute(
        "stat", {"path": "sepianai.py"}))
    print()
    print("count_lines sepianai.py ->", p.execute(
        "count_lines", {"path": "sepianai.py"}))
    print()
    print("head(20) sepianai.py ->",
          {k: v for k, v in p.execute(
              "head", {"path": "sepianai.py", "n": 20}
          ).items() if k != "content"})
    print()
    print("grep 'def _' in time_plugin.py ->")
    r = p.execute("grep", {
        "path": "plugins/time_plugin.py",
        "pattern": r"^def |^    def ",
        "context": 0,
        "limit": 20,
    })
    print({k: v for k, v in r.items() if k != "content"})
    print(r.get("content", "")[:1000])
