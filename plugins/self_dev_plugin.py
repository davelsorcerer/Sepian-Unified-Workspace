#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
self_dev_plugin.py — Approval-gated code-editing plugin for Sepian.

All write operations require explicit human authorization. The plugin NEVER
talks to Tkinter directly; instead it calls a callback supplied by the main
app, which routes the approval request to the Tk main thread. This avoids
the cross-thread Tk issues that break ApprovedShellPlugin.

Approval flow:
    1. Model emits propose_edit / run_test.
    2. Plugin queues the request and calls self._approval_callback(payload).
    3. Main app shows modal (or chat fallback), gets user decision.
    4. Main app invokes approve_edit / reject_edit / apply_edit on this
       plugin via the tool-call loop, which executes the queued request.

The plugin never executes a queued action without an explicit
approve_edit or apply_edit call from the user-approved modal result.
"""

import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

try:
    from sepian_plugin import SepianPlugin
except Exception:
    SepianPlugin = object  # allow standalone testing


# Shell metachars we forbid (use shlex + shell=False).
SHELL_METACHARS = set(";|&`$()<>*?[]{}!\n\r\t")


class SelfDevPlugin(SepianPlugin if SepianPlugin is not object else object):
    """Approval-gated file editing + test running for Sepian."""

    def __init__(self):
        self.name = "SelfDevPlugin"
        self.enabled = False  # default OFF; turned on by config dev_mode_enabled
        self.config = {}
        self.status_callback = None
        self._app = None
        # Approval callback: fn(payload_dict) -> result_dict
        # The main app sets this so we can hand UI requests back to the main
        # thread. If unset, every request returns an error instead of hanging.
        self._approval_callback = None
        self.set_config(self.get_default_config())

        # Thread-safe queue of pending requests awaiting user approval
        self._pending_lock = threading.Lock()
        self._pending = {}  # edit_id -> payload dict

        # Session id — only approve_edit calls with edit_ids from THIS session
        # are honored. Prevents the model from replaying old ids.
        self._session_id = uuid.uuid4().hex

        # Audit log
        self._audit_path = Path.home() / "sepian_server_mount" / "self_dev_audit.jsonl"
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Snapshot directory
        self._snapshot_root = Path.home() / "sepian_server_mount" / "snapshots"
        try:
            self._snapshot_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # SepianPlugin interface
    # ------------------------------------------------------------------ #

    def get_description(self):
        return ("Self-development plugin: read/search files, propose edits, "
                "and run tests. EVERY write/test requires human approval via "
                "a modal dialog.")

    def get_commands(self):
        return [
            "list_files",
            "read_file",
            "search_code",
            "propose_edit",
            "write_file",
            "list_pending",
            "approve_edit",
            "reject_edit",
            "apply_pending",
            "run_test",
            "snapshot_create",
            "snapshot_list",
            "snapshot_restore",
        ]

    def get_default_config(self):
        workspace = "/home/davel/Public/Sepian-Unified-Workspace"
        return {
            "allowed_paths": [workspace],
            "deny_paths": [
                os.path.join(workspace, "__pycache__"),
                os.path.join(workspace, "plugins", "__pycache__"),
            ],
            "max_file_bytes": 262144,         # 256 KB per file edit
            "max_diff_lines": 1500,           # refuse huge diffs
            "max_output_size": 50000,         # cap stdout/stderr from tests
            "default_test_timeout": 30,
            "max_test_timeout": 180,
            "audit_enabled": True,
            "auto_snapshot": True,
        }

    def set_config(self, config):
        self.config = dict(self.get_default_config())
        if config:
            self.config.update(config)

    def on_config_update(self):
        pass

    def set_app(self, app):
        self._app = app

    def set_approval_callback(self, fn):
        """Main app wires its modal handler here. fn(payload) -> result."""
        self._approval_callback = fn

    # ------------------------------------------------------------------ #
    # Audit
    # ------------------------------------------------------------------ #

    def _audit(self, event, **fields):
        if not self.config.get("audit_enabled", True):
            return
        try:
            entry = {
                "ts": datetime.utcnow().isoformat() + "Z",
                "session": self._session_id,
                "event": event,
                **fields,
            }
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"[SelfDev] audit write failed: {e}", flush=True)

    # ------------------------------------------------------------------ #
    # Path sandbox
    # ------------------------------------------------------------------ #

    def _resolve_allowed(self, path_str):
        """Resolve a path against allowed_paths. Returns (ok, resolved_or_err)."""
        if not path_str:
            return False, "empty path"
        # Reject traversal in the raw string first
        if ".." in Path(path_str).parts:
            return False, "path contains '..' traversal segment"
        try:
            p = Path(path_str).expanduser().resolve()
        except Exception as e:
            return False, f"could not resolve path: {e}"
        allowed = self.config.get("allowed_paths") or []
        if not allowed:
            return False, "no allowed_paths configured"
        for ap in allowed:
            try:
                allowed_resolved = Path(ap).expanduser().resolve()
            except Exception:
                continue
            if p == allowed_resolved or allowed_resolved in p.parents:
                # also check deny_paths
                for dp in self.config.get("deny_paths") or []:
                    try:
                        dpr = Path(dp).expanduser().resolve()
                    except Exception:
                        continue
                    if dpr in p.parents or p == dpr:
                        return False, f"path is in denied subtree: {dpr}"
                return True, p
        return False, f"path not in allowed_paths: {p}"

    # ------------------------------------------------------------------ #
    # Approval gate
    # ------------------------------------------------------------------ #

    def _request_approval(self, payload):
        """Send an approval request to the main app. Returns the result dict.
        print(f"[DEBUG] _request_approval called, callback={self._approval_callback}")

        If no callback is wired, refuse rather than hang.

        Threading note: the callback is responsible for its own thread
        scheduling (it uses master.after(0, ...) to marshal the modal to
        the Tk main thread, then blocks until the user decides). We call
        it directly rather than wrapping it in our own threading.Event —
        doing both leads to a double-block race where the second Event
        can be set after the modal closed but BEFORE Tk has finished
        processing <Destroy>, which leaves the next approval's
        registration check rejecting the new modal as a "duplicate".
        """
        if not self._approval_callback:
            return {"ok": False, "decision": "deny",
                    "error": "no approval handler registered "
                             "(SelfDevPlugin not wired into UI)"}
        try:
            result = self._approval_callback(payload) or {}
        except Exception as e:
            print(f"[SelfDev] approval callback raised: {e}", flush=True)
            return {"ok": False, "decision": "deny",
                    "error": f"approval handler error: {e}"}
        # Normalise: callback may return {"decision": "approve"} or
        # {"ok": True, "decision": "approve"}. Either way we surface
        # whatever it gave us.
        return result if isinstance(result, dict) else \
            {"ok": False, "decision": "deny",
             "error": "approval callback returned non-dict"}

    # ------------------------------------------------------------------ #
    # Diffing
    # ------------------------------------------------------------------ #

    def _make_diff(self, original, new, path):
        return "".join(difflib.unified_diff(
            original.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{path} (before)",
            tofile=f"{path} (after)",
            n=3,
        ))

    # ------------------------------------------------------------------ #
    # Snapshot helpers
    # ------------------------------------------------------------------ #

    def _snapshot_dir(self, label):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:60] or "snapshot"
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        d = self._snapshot_root / f"{ts}_{safe}"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"could not create snapshot dir: {e}")
        return d

    # ------------------------------------------------------------------ #
    # Read-only commands
    # ------------------------------------------------------------------ #

    def _cmd_list_files(self, args):
        rel = (args.get("path") or args.get("dir") or "").strip()
        ok, p = self._resolve_allowed(rel or self.config["allowed_paths"][0])
        if not ok:
            return {"ok": False, "error": p}
        max_entries = int(args.get("max_entries", 500))
        try:
            entries = []
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
                for f in files:
                    full = Path(root) / f
                    relp = full.relative_to(p)
                    entries.append(str(relp))
                    if len(entries) >= max_entries:
                        break
                if len(entries) >= max_entries:
                    break
            return {"ok": True, "path": str(p), "files": sorted(entries),
                    "count": len(entries), "truncated": len(entries) >= max_entries}
        except Exception as e:
            return {"ok": False, "error": f"list_files error: {e}"}

    def _cmd_read_file(self, args):
        path_str = (args.get("path") or "").strip()
        ok, p = self._resolve_allowed(path_str)
        if not ok:
            return {"ok": False, "error": p}
        try:
            start = int(args.get("start_line", 0))
            end = args.get("end_line")
            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            lo = max(0, start)
            hi = len(lines) if end is None else min(len(lines), int(end) + 1)
            sliced = lines[lo:hi]
            return {"ok": True, "path": str(p),
                    "total_lines": len(lines),
                    "start_line": lo, "end_line": hi - 1,
                    "content": "\n".join(sliced)}
        except Exception as e:
            return {"ok": False, "error": f"read_file error: {e}"}

    def _cmd_search_code(self, args):
        pattern = args.get("pattern", "")
        if not pattern:
            return {"ok": False, "error": "missing 'pattern'"}
        path_str = (args.get("path") or self.config["allowed_paths"][0]).strip()
        ok, p = self._resolve_allowed(path_str)
        if not ok:
            return {"ok": False, "error": p}
        regex = bool(args.get("regex", False))
        max_hits = int(args.get("max_hits", 200))
        try:
            pat = re.compile(pattern) if regex else None
            hits = []

            # Build the list of files to scan. If 'path' is a single file,
            # search just that file; otherwise walk the directory tree.
            # (os.walk on a file path silently yields nothing, which used
            # to make single-file searches return 0 hits.)
            if p.is_file():
                files_to_scan = [p]
                walk_root = p.parent
            else:
                files_to_scan = []
                for root, dirs, files in os.walk(p):
                    dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
                    for f in files:
                        files_to_scan.append(Path(root) / f)
                walk_root = p

            for full in files_to_scan:
                try:
                    rel = str(full.relative_to(walk_root))
                except ValueError:
                    rel = str(full)
                try:
                    for ln, line in enumerate(full.read_text(
                            encoding="utf-8", errors="replace").splitlines(),
                            start=1):
                        match = (pat.search(line) if pat
                                 else pattern in line)
                        if match:
                            hits.append({
                                "file": rel,
                                "line": ln,
                                "text": line.rstrip(),
                            })
                            if len(hits) >= max_hits:
                                break
                except Exception:
                    continue
                if len(hits) >= max_hits:
                    break
            return {"ok": True, "pattern": pattern, "hits": hits,
                    "count": len(hits), "truncated": len(hits) >= max_hits}
        except Exception as e:
            return {"ok": False, "error": f"search_code error: {e}"}

    # ------------------------------------------------------------------ #
    # propose_edit — dry-run only
    # ------------------------------------------------------------------ #

    def _cmd_propose_edit(self, args):
        path_str = (args.get("path") or "").strip()
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        rationale = (args.get("rationale") or "").strip()
        create_if_missing = bool(args.get("create_if_missing", False))

        if not path_str:
            return {"ok": False, "error": "missing 'path'"}

        ok, p = self._resolve_allowed(path_str)
        if not ok:
            return {"ok": False, "error": p}

        if "__pycache__" in p.parts:
            return {"ok": False, "error": "refusing to edit __pycache__"}

        file_exists = p.exists() and p.is_file()
        is_new_file = (not file_exists) and create_if_missing
        # create_if_missing=True is ALSO the explicit "I want a full
        # overwrite" signal from the write_file API. When the file
        # already exists, treat the request as an overwrite of the whole
        # file rather than rejecting it. The caller is still required to
        # supply a non-empty old_text that matches the existing file (so
        # the diff is human-readable and the apply step is safe), and the
        # approval modal still gates the actual write.
        is_overwrite = create_if_missing and file_exists
        if is_overwrite:
            # Re-classify: not a new file, but a full-file replace.
            is_new_file = False

        # For edits to EXISTING files (including overwrites), old_text
        # must be non-empty (we require a unique anchor to avoid blind
        # overwrites). For new-file creation, old_text must be empty.
        if not is_new_file and old_text == "":
            return {"ok": False,
                    "error": ("propose_edit on an existing file requires "
                              "'old_text' (unique anchor). For NEW files, "
                              "set create_if_missing=True and old_text=\"\". "
                              "Refusing to blind-write an existing file.")}
        if is_new_file and old_text != "":
            return {"ok": False,
                    "error": ("create_if_missing=True requires old_text=\"\" "
                              "(you can't anchor a brand-new file).")}

        # Read existing content if we're editing an existing file.
        original = ""
        if not is_new_file:
            try:
                original = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return {"ok": False, "error": f"could not read file: {e}"}

            count = original.count(old_text)
            if count == 0:
                return {"ok": False,
                        "error": "old_text not found in file — refusing to "
                                 "write a blind replacement (would corrupt "
                                 "the file)"}
            if count > 1:
                return {"ok": False,
                        "error": (f"old_text matches {count} places in the "
                                  "file. Narrow it to a unique block.")}
            new_content = original.replace(old_text, new_text, 1)
        else:
            # New file: just write new_text verbatim. We don't auto-create
            # parent directories here; if the user wants them, they can
            # pass `mkdir_parents=True` (defaults to True for new files).
            new_content = new_text

        if len(new_content.encode("utf-8")) > self.config["max_file_bytes"]:
            return {"ok": False,
                    "error": f"result would exceed "
                             f"{self.config['max_file_bytes']} bytes"}

        diff = self._make_diff(original, new_content, str(p))
        if not diff.strip():
            return {"ok": False, "error": "no change produced"}
        if len(diff.splitlines()) > self.config["max_diff_lines"]:
            return {"ok": False,
                    "error": f"diff too large "
                             f"({len(diff.splitlines())} lines, "
                             f"max {self.config['max_diff_lines']})"}

        edit_id = uuid.uuid4().hex[:12]
        payload = {
            "kind": "propose_new_file" if is_new_file else "propose_edit",
            "edit_id": edit_id,
            "session": self._session_id,
            "path": str(p),
            "rationale": rationale,
            "diff": diff,
            "old_text": old_text,
            "new_text": new_text,
            "is_new_file": is_new_file,
        }
        with self._pending_lock:
            self._pending[edit_id] = payload

        self._audit(
            "PROPOSE_NEW_FILE" if is_new_file else "PROPOSE",
            edit_id=edit_id, path=str(p),
            rationale=rationale, diff_lines=len(diff.splitlines()),
            new_file_bytes=len(new_content.encode("utf-8"))
                            if is_new_file else 0,
        )

        # Single-step approval: queue, then immediately open the modal and
        # wait for the user's decision. On Approve, apply the edit and
        # return the result. On Deny/timeout, drop the pending entry and
        # return an error. This matches ApprovedShellPlugin.run_command's
        # behavior (one tool call -> one modal -> done) so small local
        # models don't have to remember a follow-up approve_edit call.
        try:
            decision = self._request_approval(payload)
            if not isinstance(decision, dict) or decision.get("decision") != "approve":
                # Drop the pending entry; user can re-propose if they change
                # their mind. Don't call reject_edit here (no audit noise).
                with self._pending_lock:
                    self._pending.pop(edit_id, None)
                reason = (decision or {}).get("error", "denied by user")
                self._audit("REJECT",
                            edit_id=edit_id, reason=reason)
                return {"ok": False, "edit_id": edit_id,
                        "error": f"denied: {reason}",
                        "denied": True}
            # Approved — apply now. _apply_edit re-validates the sandbox.
            applied = self._apply_edit(edit_id, payload)
            if applied.get("ok"):
                applied["edit_id"] = edit_id
                applied["auto_applied"] = True
            else:
                # Apply failed (sandbox race, IO error, etc.). Keep the
                # pending entry so the user can retry via the Pending dialog.
                applied["edit_id"] = edit_id
            return applied
        except Exception as e:
            with self._pending_lock:
                self._pending.pop(edit_id, None)
            return {"ok": False, "edit_id": edit_id,
                    "error": f"approval flow error: {e}"}

    # ------------------------------------------------------------------ #
    # write_file — simplified file-write API
    # ------------------------------------------------------------------ #
    #
    # The model kept trying to use ApprovedShellPlugin.run_command with
    # `echo ... > file` for simple file writes, and the shell plugin
    # rightly rejected shell redirects. propose_edit requires the model
    # to think about old_text anchors and create_if_missing, which is too
    # much ceremony for "write a story to a file". This command is a
    # thin wrapper that takes (path, content) and queues an approval.
    #
    # Behavior:
    #   * If the file does not exist: queues a NEW-FILE create with the
    #     full content.
    #   * If the file already exists: queues an APPEND/REPLACE that shows
    #     a diff of the new content vs. the old content.
    #   * The user still has to approve via the modal — this is just a
    #     cleaner API for the model to use.
    #
    # Returns the same shape as propose_edit (ok, edit_id, path, diff,
    # is_new_file, message) so the rest of the system can handle it
    # uniformly.

    def _cmd_write_file(self, args):
        path_str = (args.get("path") or "").strip()
        content = args.get("content", "")
        rationale = (args.get("rationale") or "").strip()
        # mode: "overwrite" (default) replaces the file; "append" keeps
        # existing content and adds new content at the end.
        mode = (args.get("mode") or "overwrite").strip().lower()
        if mode not in ("overwrite", "append"):
            return {"ok": False, "error": f"unknown mode '{mode}' "
                                          "(use 'overwrite' or 'append')"}

        if not path_str:
            return {"ok": False, "error": "missing 'path'"}

        ok, p = self._resolve_allowed(path_str)
        if not ok:
            return {"ok": False, "error": p}
        if "__pycache__" in p.parts:
            return {"ok": False, "error": "refusing to edit __pycache__"}

        file_exists = p.exists() and p.is_file()
        is_new_file = (not file_exists)

        if is_new_file:
            new_content = content
            old_text = ""
        elif mode == "append":
            try:
                original = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return {"ok": False, "error": f"could not read file: {e}"}
            if not original.endswith("\n"):
                original = original + "\n"
            new_content = original + content
            # Use the last line of the file as the unique anchor so the
            # diff is small and human-readable.
            old_text = (original.rstrip().splitlines() or [""])[-1]
        else:  # overwrite
            try:
                original = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return {"ok": False, "error": f"could not read file: {e}"}
            # Show the whole-file diff; old_text is the entire file
            # contents so propose_edit's uniqueness check works
            # (count==1).
            old_text = original
            new_content = content

        # Validate the resulting edit through the same path as
        # propose_edit so all the existing checks (size, sandbox,
        # snapshot) apply. We call _cmd_propose_edit with the right
        # args so we get identical behavior.
        return self._cmd_propose_edit({
            "path": str(p),
            "old_text": old_text,
            "new_text": new_content,
            "rationale": rationale or f"write_file ({mode})",
            "create_if_missing": True,  # OK because file_exists is
                                        # re-checked in _apply_edit
        })

    # ------------------------------------------------------------------ #
    # approve_edit / reject_edit
    # ------------------------------------------------------------------ #

    def _cmd_approve_edit(self, args):
        edit_id = args.get("edit_id", "")
        if not edit_id:
            return {"ok": False, "error": "missing 'edit_id'"}
        with self._pending_lock:
            payload = self._pending.get(edit_id)
        if not payload:
            return {"ok": False, "error": f"no pending edit with id {edit_id}"}
        if payload.get("session") != self._session_id:
            return {"ok": False,
                    "error": "edit_id belongs to a different session"}

        approval_payload = dict(payload)
        approval_payload["kind"] = "approve_edit"
        approval_payload["phase"] = "final_approval"
        result = self._request_approval(approval_payload)
        decision = (result or {}).get("decision", "deny")
        if decision != "approve":
            self._audit("REJECT", edit_id=edit_id,
                        reason=(result or {}).get("error", "user denied"))
            with self._pending_lock:
                self._pending.pop(edit_id, None)
            return {"ok": False, "decision": decision,
                    "error": (result or {}).get("error", "denied by user")}

        return self._apply_edit(edit_id, payload)

    def _cmd_apply_pending(self, args):
        """Apply a pending edit without re-prompting in a modal.

        Use this when the caller already showed the diff and got the
        user's approval (e.g. when the Pending button opens its own
        pre-modal and the user clicks Approve there). Calling
        _cmd_approve_edit in that case would open a SECOND modal that
        either gets hidden behind the first or simply re-runs the
        approval flow.

        Audited as APPLY so the normal log trail still records who/what.
        """
        edit_id = args.get("edit_id", "")
        if not edit_id:
            return {"ok": False, "error": "missing 'edit_id'"}
        with self._pending_lock:
            payload = self._pending.get(edit_id)
        if not payload:
            return {"ok": False,
                    "error": f"no pending edit with id {edit_id}"}
        if payload.get("session") != self._session_id:
            return {"ok": False,
                    "error": "edit_id belongs to a different session"}
        self._audit("APPROVE_VIA_UI", edit_id=edit_id,
                    path=payload.get("path"))
        return self._apply_edit(edit_id, payload)

    def _cmd_reject_edit(self, args):
        edit_id = args.get("edit_id", "")
        if not edit_id:
            return {"ok": False, "error": "missing 'edit_id'"}
        with self._pending_lock:
            payload = self._pending.pop(edit_id, None)
        if not payload:
            return {"ok": False, "error": f"no pending edit with id {edit_id}"}
        self._audit("REJECT", edit_id=edit_id, reason="explicit reject")
        return {"ok": True, "edit_id": edit_id, "rejected": True}

    def _apply_edit(self, edit_id, payload):
        path = Path(payload["path"])
        old_text = payload["old_text"]
        new_text = payload["new_text"]
        is_new_file = bool(payload.get("is_new_file", False))

        # Re-resolve and confirm sandbox (defense in depth).
        ok, p = self._resolve_allowed(str(path))
        if not ok:
            return {"ok": False, "error": f"path blocked at apply time: {p}"}

        new_content = None
        if is_new_file:
            # New file: file must still not exist (model/UX may have raced).
            if p.exists():
                return {"ok": False,
                        "error": ("file appeared between proposal and "
                                  "approve — refusing to overwrite. "
                                  "Re-propose with propose_edit (no "
                                  "create_if_missing).")}
            new_content = new_text
            # Ensure parent directory exists.
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return {"ok": False,
                        "error": f"could not create parent dir: {e}"}
        else:
            try:
                original = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return {"ok": False,
                        "error": f"could not re-read file: {e}"}
            if original.count(old_text) != 1:
                return {"ok": False,
                        "error": ("file has changed since proposal; old_text "
                                  "no longer matches exactly once. "
                                  "Re-propose.")}
            new_content = original.replace(old_text, new_text, 1)

        snapshot_path = None
        if self.config.get("auto_snapshot", True):
            try:
                sd = self._snapshot_dir(edit_id)
                snapshot_path = sd / path.name
                if p.exists():
                    shutil.copy2(p, snapshot_path)
                else:
                    # For brand-new files, write an empty placeholder so
                    # snapshot_restore can detect "this snapshot replaces
                    # with empty" semantics if needed.
                    snapshot_path.write_text("", encoding="utf-8")
            except Exception as e:
                return {"ok": False, "error": f"snapshot failed: {e}"}

        tmp = path.with_suffix(path.suffix + ".sepian.tmp")
        try:
            tmp.write_text(new_content, encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            try:
                tmp.unlink()
            except Exception:
                pass
            return {"ok": False,
                    "error": f"write failed: {e}",
                    "snapshot": str(snapshot_path) if snapshot_path else None}

        with self._pending_lock:
            self._pending.pop(edit_id, None)

        self._audit(
            "APPLY_NEW_FILE" if is_new_file else "APPLY",
            edit_id=edit_id, path=str(path),
            snapshot=str(snapshot_path) if snapshot_path else None,
            bytes=len(new_content.encode("utf-8")),
        )

        out = {
            "ok": True, "edit_id": edit_id, "path": str(path),
            "applied": True, "is_new_file": is_new_file,
            "snapshot": str(snapshot_path) if snapshot_path else None,
        }
        try:
            running = Path(__file__).resolve()
            if path.resolve() == running:
                out["restart_recommended"] = True
                out["restart_note"] = ("sepianai.py was modified. "
                                       "Restart Sepian to pick up changes.")
        except Exception:
            pass
        return out

    # ------------------------------------------------------------------ #
    # run_test — approval + subprocess
    # ------------------------------------------------------------------ #

    def _parse_argv(self, cmd):
        if not isinstance(cmd, str) or not cmd.strip():
            return False, "empty command"
        if any(c in SHELL_METACHARS for c in cmd):
            return False, "shell metacharacters rejected"
        try:
            argv = shlex.split(cmd, posix=True)
        except ValueError as e:
            return False, f"parse error: {e}"
        if not argv:
            return False, "empty after parsing"
        return True, argv

    def _cmd_run_test(self, args):
        cmd_str = (args.get("cmd") or args.get("command") or "").strip()
        if not cmd_str:
            return {"ok": False, "error": "missing 'cmd'"}
        cwd = (args.get("cwd") or "").strip()
        reason = (args.get("reason") or "Running test command").strip()
        try:
            timeout = int(args.get("timeout",
                                   self.config.get("default_test_timeout", 30)))
        except (TypeError, ValueError):
            timeout = int(self.config.get("default_test_timeout", 30))
        timeout = max(1, min(timeout,
                             int(self.config.get("max_test_timeout", 180))))

        ok, argv_or_err = self._parse_argv(cmd_str)
        if not ok:
            return {"ok": False, "error": f"rejected before approval: {argv_or_err}"}

        if cwd:
            ok, cwd_err = self._resolve_allowed(cwd)
            if not ok:
                return {"ok": False, "error": f"cwd rejected: {cwd_err}"}

        run_id = uuid.uuid4().hex[:12]
        payload = {
            "kind": "run_test",
            "run_id": run_id,
            "session": self._session_id,
            "cmd": cmd_str,
            "cwd": cwd,
            "timeout": timeout,
            "reason": reason,
            "argv": argv_or_err,
        }
        result = self._request_approval(payload)
        decision = (result or {}).get("decision", "deny")
        if decision != "approve":
            self._audit("TEST_REJECT", run_id=run_id, cmd=cmd_str,
                        reason=(result or {}).get("error", "denied"))
            return {"ok": False, "decision": decision,
                    "error": (result or {}).get("error", "denied by user")}

        self._audit("TEST_RUN", run_id=run_id, cmd=cmd_str,
                    cwd=cwd, timeout=timeout)
        try:
            proc = subprocess.run(
                argv_or_err,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd or None,
            )
        except subprocess.TimeoutExpired:
            self._audit("TEST_TIMEOUT", run_id=run_id, cmd=cmd_str, timeout=timeout)
            return {"ok": False, "timed_out": True,
                    "error": f"timed out after {timeout}s"}
        except Exception as e:
            self._audit("TEST_ERR", run_id=run_id, cmd=cmd_str, error=str(e))
            return {"ok": False, "error": f"execution error: {e}"}

        cap = int(self.config.get("max_output_size", 50000))
        out = proc.stdout or ""
        err = proc.stderr or ""
        trunc = False
        if len(out) > cap:
            out = out[-cap:]; trunc = True
        if len(err) > cap:
            err = err[:cap:]; trunc = True

        self._audit("TEST_DONE", run_id=run_id, cmd=cmd_str,
                    exit_code=proc.returncode, ok=proc.returncode == 0)
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": out,
            "stderr": err,
            "truncated": trunc,
        }

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #

    def _cmd_snapshot_create(self, args):
        label = (args.get("label") or "manual").strip()
        path_str = (args.get("path") or "").strip()
        if path_str:
            ok, p = self._resolve_allowed(path_str)
            if not ok:
                return {"ok": False, "error": p}
        else:
            p = Path(self.config["allowed_paths"][0])
        try:
            sd = self._snapshot_dir(label)
            if p.is_file():
                shutil.copy2(p, sd / p.name)
            else:
                for child in p.iterdir():
                    if child.is_file():
                        shutil.copy2(child, sd / child.name)
            self._audit("SNAPSHOT_CREATE", label=label, path=str(p),
                        snapshot=str(sd))
            return {"ok": True, "label": label, "path": str(p),
                    "snapshot": str(sd)}
        except Exception as e:
            return {"ok": False, "error": f"snapshot_create error: {e}"}

    def _cmd_snapshot_list(self, args):
        try:
            entries = []
            for d in sorted(self._snapshot_root.iterdir()):
                if d.is_dir():
                    files = sorted(f.name for f in d.iterdir() if f.is_file())
                    entries.append({
                        "name": d.name,
                        "path": str(d),
                        "files": files,
                        "created": d.stat().st_mtime,
                    })
            return {"ok": True, "snapshots": entries, "count": len(entries)}
        except Exception as e:
            return {"ok": False, "error": f"snapshot_list error: {e}"}

    def _cmd_snapshot_restore(self, args):
        name = (args.get("name") or args.get("label") or "").strip()
        if not name:
            return {"ok": False, "error": "missing snapshot 'name'"}
        sd = self._snapshot_root / name
        if not sd.is_dir():
            return {"ok": False, "error": f"no such snapshot: {name}"}

        payload = {
            "kind": "snapshot_restore",
            "snapshot": str(sd),
            "files": [f.name for f in sd.iterdir() if f.is_file()],
        }
        result = self._request_approval(payload)
        if (result or {}).get("decision") != "approve":
            return {"ok": False,
                    "error": (result or {}).get("error", "denied by user")}

        restored = []
        for src in sd.iterdir():
            if not src.is_file():
                continue
            ok, target = self._resolve_allowed(
                str(Path(self.config["allowed_paths"][0]) / src.name))
            if not ok:
                continue
            try:
                shutil.copy2(src, target)
                restored.append(str(target))
            except Exception as e:
                self._audit("SNAPSHOT_RESTORE_ERR", file=str(target), error=str(e))
        self._audit("SNAPSHOT_RESTORE", snapshot=str(sd), restored=restored)
        return {"ok": True, "restored": restored, "snapshot": str(sd)}

    def _cmd_list_pending(self, args):
        with self._pending_lock:
            pending = []
            for eid, p in self._pending.items():
                pending.append({
                    "edit_id": eid,
                    "path": p.get("path"),
                    "rationale": p.get("rationale"),
                    "diff_lines": len((p.get("diff") or "").splitlines()),
                })
        return {"ok": True, "pending": pending, "count": len(pending)}

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    def execute(self, command, args):
        if not self.enabled:
            return {"ok": False,
                    "error": "SelfDevPlugin is disabled. Enable dev_mode in "
                             "Settings and restart."}
        args = args or {}
        try:
            if command == "list_files":      return self._cmd_list_files(args)
            if command == "read_file":       return self._cmd_read_file(args)
            if command == "search_code":     return self._cmd_search_code(args)
            if command == "propose_edit":    return self._cmd_propose_edit(args)
            if command == "write_file":      return self._cmd_write_file(args)
            if command == "list_pending":    return self._cmd_list_pending(args)
            if command == "approve_edit":    return self._cmd_approve_edit(args)
            if command == "reject_edit":     return self._cmd_reject_edit(args)
            if command == "apply_pending":   return self._cmd_apply_pending(args)
            if command == "run_test":        return self._cmd_run_test(args)
            if command == "snapshot_create": return self._cmd_snapshot_create(args)
            if command == "snapshot_list":   return self._cmd_snapshot_list(args)
            if command == "snapshot_restore":return self._cmd_snapshot_restore(args)
            return {"ok": False, "error": f"unknown command: {command}"}
        except Exception as e:
            return {"ok": False, "error": f"plugin error: {e}"}


if __name__ == "__main__":
    p = SelfDevPlugin()
    print("Description:", p.get_description())
    print("Commands:", p.get_commands())
    print("Default config:", p.get_default_config())
