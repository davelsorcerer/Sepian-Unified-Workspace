#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
approved_shell_plugin.py - Approval-gated shell command execution.

This version does NOT touch Tkinter directly. All UI goes through an
approval callback that the main Sepian app wires up. This fixes the
"loads but doesn't work" bug caused by creating Tk widgets from the
LLM-streaming worker thread.

Public surface preserved:
    get_description, get_commands, get_default_config, set_config,
    set_app, execute, handle_voice_command, list_sticky, clear_sticky

New public surface:
    set_approval_callback(fn) -- main app wires its modal handler here.
        fn(payload: dict) -> dict with {"ok", "decision", optional "error"}
        where decision in {"approve", "sticky", "deny"}.
"""

import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

try:
    from sepian_plugin import SepianPlugin
except Exception:
    # Allow standalone import for unit testing
    SepianPlugin = object


# Characters that indicate shell-level interpretation. We reject any command
# containing these, so we can safely use shell=False and argv parsing.
SHELL_METACHARS = set(";|&`$()<>*?[]{}!\n\r\t")


class ApprovedShellPlugin(SepianPlugin if SepianPlugin is not object else object):
    """Shell plugin that requires per-command human approval via a modal dialog."""

    # ---- SepianPlugin plumbing -------------------------------------------

    def __init__(self):
        self.name = "ApprovedShellPlugin"
        self.enabled = True
        self.config = {}
        self.status_callback = None
        self._app = None
        # Approval callback: fn(payload) -> {"ok", "decision", ...}
        # Set by the main app. If None, fall back to a console prompt (which
        # only works when called from the main thread; from worker threads it
        # hangs).
        self._approval_callback = None
        self.set_config(self.get_default_config())

        # Sticky approvals: prefix-string -> expiry epoch seconds.
        self._sticky_lock = threading.Lock()
        self._sticky = {}  # {"ls -la /tmp": 1737654321.0, ...}

        # Audit log path
        self._audit_path = Path.home() / "sepian_server_mount" / "approved_shell_audit.log"
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def get_description(self):
        return ("Shell command execution with per-command human approval "
                "(modal dialog before each run)")

    def get_commands(self):
        return ["run_command", "list_approvals", "revoke_approvals", "set_sticky_window"]

    def get_default_config(self):
        return {
            "allowed_paths": [str(Path.home()), "/tmp"],
            "max_output_size": 50000,
            "default_timeout": 15,
            "max_timeout": 120,
            "sticky_enabled": False,
            "sticky_window_seconds": 300,
            "show_diff_for_modifications": True,
            "audit_enabled": True,
            "approval_timeout_seconds": 300,
        }

    def set_config(self, config):
        # Preserve any prior values; only update keys present in the new config.
        new = dict(self.get_default_config())
        if config:
            new.update(config)
        self.config = new

    def on_config_update(self):
        pass

    def set_app(self, app):
        """Called by main app when the plugin is registered."""
        self._app = app

    def set_approval_callback(self, fn):
        """Main app wires its approval-modal handler here.

        fn(payload) must return a dict like:
            {"ok": True,  "decision": "approve"}
            {"ok": True,  "decision": "sticky"}
            {"ok": False, "decision": "deny", "error": "user denied"}
        """
        self._approval_callback = fn

    # ---- Audit log -------------------------------------------------------

    def _audit(self, event, **fields):
        if not self.config.get("audit_enabled", True):
            return
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            parts = " ".join(f"{k}={self._fmt(v)}" for k, v in fields.items())
            line = f"{ts} {event} {parts}\n"
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            # Audit must never crash the plugin
            print(f"[ApprovedShell] audit write failed: {e}", flush=True)

    @staticmethod
    def _fmt(v):
        s = str(v)
        if any(c.isspace() for c in s) or '"' in s:
            return '"' + s.replace('"', '\\"') + '"'
        return s

    # ---- Sticky-approval helpers -----------------------------------------

    def _sticky_key(self, argv):
        """First 3 tokens become the sticky matching key."""
        if not argv:
            return ""
        return " ".join(shlex.quote(a) for a in argv[:3])

    def _is_sticky_approved(self, argv):
        if not self.config.get("sticky_enabled", False):
            return False
        key = self._sticky_key(argv)
        if not key:
            return False
        now = time.time()
        with self._sticky_lock:
            expiry = self._sticky.get(key)
            if expiry is None:
                return False
            if expiry < now:
                self._sticky.pop(key, None)
                return False
            return True

    def _add_sticky(self, argv, seconds):
        if seconds <= 0:
            return
        key = self._sticky_key(argv)
        if not key:
            return
        with self._sticky_lock:
            self._sticky[key] = time.time() + seconds

    def list_sticky(self):
        now = time.time()
        with self._sticky_lock:
            return {
                k: round(v - now, 1)
                for k, v in self._sticky.items()
                if v > now
            }

    def clear_sticky(self):
        with self._sticky_lock:
            n = len(self._sticky)
            self._sticky.clear()
            return n

    # ---- Security checks -------------------------------------------------

    def _has_shell_metachars(self, s):
        return any(c in SHELL_METACHARS for c in s)

    def _validate_cwd(self, cwd):
        if not cwd:
            return True, ""
        allowed = self.config.get("allowed_paths") or []
        if not allowed:
            return True, ""
        try:
            resolved = Path(cwd).expanduser().resolve()
        except Exception as e:
            return False, f"invalid cwd: {e}"
        for ap in allowed:
            try:
                allowed_path = Path(ap).expanduser().resolve()
            except Exception:
                continue
            if resolved == allowed_path:
                return True, ""
            cur = resolved
            while cur != cur.parent:
                if cur == allowed_path:
                    return True, ""
                cur = cur.parent
        return False, f"cwd not in allowed_paths: {resolved}"

    def _parse_argv(self, cmd):
        """Split a command string into argv. Reject shell metachars or bad quoting."""
        if not isinstance(cmd, str) or not cmd.strip():
            return False, "empty command"
        if self._has_shell_metachars(cmd):
            # Special-case the very common "echo X > file" / "echo X >> file"
            # / "tee file" patterns. The shell plugin refuses shell redirects
            # by design, but the model keeps trying. Point it at the right
            # tool so it can self-correct on the next turn.
            cmd_strip = cmd.strip()
            extra_hint = ""
            if (">" in cmd_strip or ">>" in cmd_strip or
                    re.search(r"\b(tee|cp|cat\s+>>|dd\s+of=)\b", cmd_strip)):
                extra_hint = (
                    " The command looks like a FILE WRITE or FILE APPEND. "
                    "Use SelfDevPlugin.propose_edit instead: for a new file, "
                    "set create_if_missing=True, old_text=\"\", "
                    "new_text=<full contents>; for an existing file, anchor "
                    "with old_text and put the new content in new_text."
                )
            return False, ("shell metacharacters are not allowed "
                           "(rejected: ; | & ` $ ( ) < > * ? [ ] { } !). "
                           "ApprovedShellPlugin runs commands directly with "
                           "shell=False, so redirects (>, >>), pipes (|), and "
                           "command chaining (; &) are forbidden."
                           + extra_hint)
        try:
            argv = shlex.split(cmd, posix=True)
        except ValueError as e:
            return False, f"could not parse command: {e}"
        if not argv:
            return False, "empty command after parsing"
        return True, argv

    # ---- Approval gate (no Tk here) -------------------------------------

    def _ask_approval(self, cmd, cwd, timeout, reason=None):
        """Get a decision from the main app's approval UI.

        Returns one of: "approve", "sticky", "deny".

        Behavior:
          * If an approval callback is wired, use it. The callback runs the
            Tk modal on the main thread; we wait (with timeout) here.
          * If no callback is wired AND we're on the main thread, fall back
            to a console input() prompt.
          * Otherwise (worker thread + no callback), refuse with deny.
            We NEVER hang the LLM worker thread.
        """
        payload = {
            "kind": "shell_command",
            "plugin": "ApprovedShellPlugin",
            "command": "run_command",
            "cmd": cmd,
            "cwd": cwd,
            "timeout": timeout,
            "reason": reason,
        }

        if self._approval_callback is not None:
            # The callback is responsible for its own thread scheduling
            # (it uses self.master.after(0, ...) to marshal the modal to
            # the Tk main thread, then blocks until the user decides).
            # Calling it again from a worker thread without re-scheduling
            # would either (a) build Tk widgets off the main thread, or
            # (b) double-block on its own internal Event.
            try:
                result = self._approval_callback(payload) or {}
            except Exception as e:
                print(f"[ApprovedShell] approval callback raised: {e}",
                      flush=True)
                return "deny"
            return result.get("decision", "deny")

        # No callback. Only safe to use console from the main thread.
        if threading.current_thread() is threading.main_thread():
            return self._console_approval(cmd, cwd, timeout, reason)

        # Worker thread + no callback -> refuse, don't hang.
        print("[ApprovedShell] no approval callback registered and called "
              "from a worker thread; refusing command. Wire "
              "set_approval_callback() in the main app to enable shell "
              "commands.", flush=True)
        return "deny"

    def _console_approval(self, cmd, cwd, timeout, reason):
        """Fallback for headless / unconfigured runs (main thread only)."""
        print("\n" + "=" * 60)
        print("Sepian: Approval Required for Shell Command")
        print("=" * 60)
        print(f"Command: {cmd}")
        print(f"CWD: {cwd or '(default)'}")
        print(f"Timeout: {timeout}s")
        if reason:
            print(f"Reason: {reason}")
        print("-" * 60)
        print("Options:")
        print("  [1] Approve once")
        if self.config.get("sticky_enabled", False):
            print(f"  [2] Approve + sticky ({self.config.get('sticky_window_seconds', 300)}s)")
        print("  [3] Deny")
        print("=" * 60)
        try:
            choice = input("Enter your choice (1/2/3): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("(no input; denying)")
            return "deny"
        if choice == "1":
            return "approve"
        if choice == "2" and self.config.get("sticky_enabled", False):
            return "sticky"
        return "deny"

    # ---- Command execution -----------------------------------------------

    def _execute(self, argv, cwd, timeout):
        try:
            proc = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd or None,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"timed out after {timeout}s",
                "timed_out": True,
            }
        except FileNotFoundError as e:
            return {"ok": False, "error": f"executable not found: {e}"}
        except Exception as e:
            return {"ok": False, "error": f"execution error: {e}"}

        cap = int(self.config.get("max_output_size", 50000))
        out = (proc.stdout or "")
        err = (proc.stderr or "")
        truncated = False
        if len(out) > cap:
            out = out[-cap:]
            truncated = True
        if len(err) > cap:
            err = err[:cap]
            truncated = True

        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": out,
            "stderr": err,
            "truncated": truncated,
            "argv": argv,
            "cwd": cwd or "",
        }

    # ---- Plugin command dispatch -----------------------------------------

    def execute(self, command, args):
        try:
            if command == "run_command":
                return self._cmd_run_command(args or {})
            if command == "list_approvals":
                return {"ok": True, "sticky_approvals": self.list_sticky()}
            if command == "revoke_approvals":
                n = self.clear_sticky()
                self._audit("REVOKE_STICKY", count=n)
                return {"ok": True, "revoked": n}
            if command == "set_sticky_window":
                secs = int((args or {}).get("seconds", 0))
                self.config["sticky_window_seconds"] = max(0, secs)
                self.config["sticky_enabled"] = secs > 0
                self._audit("STICKY_WINDOW", seconds=secs)
                return {"ok": True,
                        "sticky_enabled": self.config["sticky_enabled"],
                        "sticky_window_seconds": self.config["sticky_window_seconds"]}
            return {"ok": False, "error": f"unknown command: {command}"}
        except Exception as e:
            return {"ok": False, "error": f"plugin error: {e}"}

    def _cmd_run_command(self, args):
        cmd_str = (args.get("cmd") or args.get("command") or "").strip()
        cwd = (args.get("cwd") or "").strip()
        reason = (args.get("reason") or "").strip()
        try:
            timeout = int(args.get("timeout",
                                   self.config.get("default_timeout", 15)))
        except (TypeError, ValueError):
            timeout = int(self.config.get("default_timeout", 15))
        timeout = max(1, min(timeout, int(self.config.get("max_timeout", 120))))

        if not cmd_str:
            return {"ok": False, "error": "missing 'cmd' argument"}

        ok, argv_or_err = self._parse_argv(cmd_str)
        if not ok:
            self._audit("REJECT", cmd=cmd_str, reason=argv_or_err)
            return {"ok": False, "error": f"rejected before approval: {argv_or_err}"}

        ok, cwd_err = self._validate_cwd(cwd)
        if not ok:
            self._audit("REJECT", cmd=cmd_str, reason=cwd_err)
            return {"ok": False, "error": f"rejected before approval: {cwd_err}"}

        if not shutil.which(argv_or_err[0]) and not Path(argv_or_err[0]).exists():
            self._audit("REJECT", cmd=cmd_str, reason="executable not on PATH")
            return {"ok": False,
                    "error": (f"rejected before approval: '{argv_or_err[0]}' "
                              f"not found on PATH")}

        # Sticky check first — silently auto-approve matching commands
        if self._is_sticky_approved(argv_or_err):
            self._audit("AUTO_RUN_STICKY", cmd=cmd_str, cwd=cwd, timeout=timeout)
            res = self._execute(argv_or_err, cwd, timeout)
            self._audit("AUTO_DONE_STICKY",
                        cmd=cmd_str, exit_code=res.get("exit_code", -1),
                        ok=res.get("ok", False))
            return res

        # Otherwise, prompt
        choice = self._ask_approval(cmd_str, cwd, timeout, reason=reason)

        if choice == "deny":
            self._audit("DENY", cmd=cmd_str, cwd=cwd, timeout=timeout)
            return {"ok": False, "error": "denied by user", "denied": True}

        if choice in ("approve", "sticky"):
            self._audit("APPROVE", cmd=cmd_str, cwd=cwd, timeout=timeout,
                        mode=choice)
            if choice == "sticky":
                self._add_sticky(argv_or_err,
                                 int(self.config.get("sticky_window_seconds", 300)))
            res = self._execute(argv_or_err, cwd, timeout)
            self._audit("RUN_DONE", cmd=cmd_str,
                        exit_code=res.get("exit_code", -1),
                        ok=res.get("ok", False),
                        timed_out=res.get("timed_out", False))
            return res

        # Unreachable but safe
        return {"ok": False, "error": "no decision made"}

    # ---- Voice (optional) -------------------------------------------------

    def handle_voice_command(self, text):
        return None


# Allow `python -m plugins.approved_shell_plugin` smoke test
if __name__ == "__main__":
    p = ApprovedShellPlugin()
    print("Description:", p.get_description())
    print("Commands:", p.get_commands())
    print("Default config:", p.get_default_config())
