#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import subprocess
import threading
import sys
import json


class ManagerPlugin:
    """
    Plugin to manage the external person detector and trigger greetings/wake.
    Automatically binds to the main app via set_app() OR by importing the main module
    if the plugin system doesn't call set_app().
    """

    name = "manager"
    version = "0.1.0"

    def __init__(self):
        # These may be set by the main app via set_app() or via import fallback
        self._app = None
        self._tts = None
        self._voice = None
        self._head = None
        self._late_bound_logged = False

        # Sub-process that runs the external detector
        self._detector_proc = None

        # Path to the signal file written by the detector
        self._signal_file = (
            "/home/davel/Sepian-Unified-Workspace/sepian_person_signal"
        )

        # Control flags for the monitor thread
        self._running = False
        self._monitor_thread = None

        # State that prevents repeated greetings for the same detection
        self._greeted = False
        self._pending_detection = False  # True if we saw a detection while unbound
        self._last_gaze = None
        self._lock = threading.Lock()

        # Track when we last tried to bind via import (to avoid spamming)
        self._last_bind_attempt = 0
        self._bind_attempt_interval = 5.0  # seconds

        # Start detector and monitor immediately
        self._start_detector()
        self._start_monitor()

    # ------------------------------------------------------------------ #
    # Required by plugin_manager.py
    # ------------------------------------------------------------------ #
    def get_description(self):
        return "Manage external person detector to trigger greetings and wake"

    def get_commands(self):
        return ["manager:status", "manager:reset_greet", "manager:stop"]

    # ------------------------------------------------------------------ #
    # Optional helpers called by the main SepianApp
    # ------------------------------------------------------------------ #
    def register(self, app):
        self.set_app(app)

    def set_app(self, app):
        """Called by the main app once the plugin is loaded."""
        self._app = app
        self._tts = getattr(app, "tts", None)
        self._voice = getattr(app, "voice", None)
        self._head = getattr(app, "head", None)

        print(
            f"[MANAGER] Bound to Sepian (tts={self._tts is not None}, "
            f"voice={self._voice is not None})"
        )

        # If the detector died while we were waiting for the app, restart it.
        if (
            self._detector_proc is None
            or self._detector_proc.poll() is not None
        ):
            print("[MANAGER] Detector not running, restarting...")
            self._start_detector()

        # If we had a detection while we were unbound, handle it now.
        if self._pending_detection:
            self._pending_detection = False
            self._greet_and_listen()

    def get_all_commands(self):
        return self.get_commands()

    def try_voice_command(self, text):
        return None

    def execute_command(self, plugin, command, args):
        if plugin != "manager":
            return {"ok": False, "error": "Unknown plugin"}

        try:
            if command == "status":
                return {
                    "ok": True,
                    "running": self._running,
                    "detector_pid": (
                        self._detector_proc.pid if self._detector_proc else None
                    ),
                    "detector_alive": (
                        self._detector_proc.poll() is None
                        if self._detector_proc
                        else False
                    ),
                    "signal_file": self._signal_file,
                    "signal_exists": os.path.exists(self._signal_file),
                    "greeted": self._greeted,
                    "sepian_bound": self._app is not None,
                }
            if command == "reset_greet":
                with self._lock:
                    self._greeted = False
                    self._pending_detection = False
                return {"ok": True, "msg": "Greet flag reset"}
            if command == "stop":
                self.shutdown()
                return {"ok": True, "msg": "Stopped"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"Unknown command: {command}"}

    # ------------------------------------------------------------------ #
    # Detector lifecycle
    # ------------------------------------------------------------------ #
    def _start_detector(self):
        script_path = os.path.join(os.path.dirname(__file__), "person_detector.py")
        python_path = "/home/davel/anaconda3/bin/python3"

        if not os.path.isfile(script_path):
            print(f"[MANAGER] ERROR: {script_path} not found")
            return
        if not os.path.isfile(python_path):
            print(f"[MANAGER] ERROR: {python_path} not found")
            return

        # Clean up any existing process first
        if self._detector_proc and self._detector_proc.poll() is None:
            try:
                self._detector_proc.terminate()
            except Exception:
                pass

        # Log detector output to a file so we can see what it's doing
        try:
            log_path = os.path.join(os.path.dirname(__file__), "detector.log")
            log_file = open(log_path, "a")
            self._detector_proc = subprocess.Popen(
                [python_path, script_path],
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            print(f"[MANAGER] Detector started (PID {self._detector_proc.pid})")
        except Exception as e:
            print(f"[MANAGER] Failed to start detector: {e}")
            self._detector_proc = None

    def _start_monitor(self):
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_signal, daemon=True
        )
        self._monitor_thread.start()

    # ------------------------------------------------------------------ #
    # Helpers: try to bind to the main app
    # ------------------------------------------------------------------ #
    def _attempt_bind_via_import(self):
        """Try to bind to the main app by importing the main module.
        Only safe to call if the main module is already loaded (avoids import loops)."""
        if self._app is not None and self._tts is not None and self._voice is not None:
            return True

        now = time.time()
        if now - self._last_bind_attempt < self._bind_attempt_interval:
            return False
        self._last_bind_attempt = now

        # Try the named module first, then __main__ (when script run directly)
        for module_name in ("sepanai", "__main__"):
            if module_name in sys.modules:
                try:
                    mod = sys.modules[module_name]
                    if hasattr(mod, "app"):
                        app = mod.app
                        self._app = app
                        self._tts = getattr(app, "tts", None)
                        self._voice = getattr(app, "voice", None)
                        self._head = getattr(app, "head", None)

                        bound_success = self._tts is not None and self._voice is not None
                        if bound_success:
                            print(
                                f"[MANAGER] Bound to Sepian via {module_name} "
                                f"(tts={self._tts is not None}, voice={self._voice is not None})"
                            )
                            if self._pending_detection:
                                self._pending_detection = False
                                self._greet_and_listen()
                        return bound_success
                except Exception as e:
                    print(f"[MANAGER] Error binding via {module_name}: {e}")
        return False

    def _ensure_app_bound(self):
        """Keep trying to get tts/voice from the app until they're available.
        Handles the case where set_app() runs before TTSManager is constructed,
        and where VoiceManager is only created when the user clicks 'Listen'."""
        # First try the import fallback if we don't have the app yet
        if self._app is None:
            if self._attempt_bind_via_import():
                return True
            return False

        # We have the app reference. Refresh tts/voice in case they just appeared.
        if self._tts is None:
            self._tts = getattr(self._app, "tts", None)
        if self._voice is None:
            self._voice = getattr(self._app, "voice", None)
        if self._head is None:
            self._head = getattr(self._app, "head", None)

        # tts must exist before we can do anything
        if self._tts is None:
            return False

        # If we just got tts (and maybe voice), log it and flush pending detection
        if not self._late_bound_logged:
            print(
                f"[MANAGER] Late-bound to tts/voice "
                f"(tts={self._tts is not None}, voice={self._voice is not None})"
            )
            self._late_bound_logged = True
            if self._pending_detection:
                self._pending_detection = False
                self._greet_and_listen()

        return True

    # ------------------------------------------------------------------ #
    # Core monitoring loop – watches the signal file
    # ------------------------------------------------------------------ #
    def _monitor_signal(self):
        print("[MANAGER] Monitor active – watching signal file")
        last_exists = None
        while self._running:
            try:
                exists = os.path.exists(self._signal_file)

                # Only log on transitions (no per-tick spam)
                if exists != last_exists:
                    if exists:
                        print("[MONITOR] signal file appeared", flush=True)
                    else:
                        print("[MONITOR] signal file gone", flush=True)
                    last_exists = exists

                if not exists:
                    # No signal → person left. Reset state and drop voice back to wake mode.
                    with self._lock:
                        was_active = self._greeted or self._pending_detection
                        self._greeted = False
                        self._pending_detection = False
                    self._set_gaze("center")
                    if self._voice and self._voice.get_mode() == self._voice.ACTIVE_LISTEN:
                        print("[MONITOR] person left → dropping voice to WAKE_LOOP", flush=True)
                        self._voice.set_mode(self._voice.WAKE_LOOP)
                    if was_active:
                        print("[MONITOR] state cleared", flush=True)
                    time.sleep(0.4)
                    continue

                # File exists → make sure we're bound to tts/voice and head.
                if not self._ensure_app_bound():
                    if not self._pending_detection:
                        print("[MONITOR] not bound → pending detection", flush=True)
                    self._pending_detection = True
                    time.sleep(0.4)
                    continue

                # We are bound and person is here
                self._set_gaze(self._read_gaze())
                with self._lock:
                    greeted = self._greeted

                if not greeted:
                    with self._lock:
                        self._greeted = True
                    print("[MONITOR] NEW detection → greeting", flush=True)
                    self._greet_and_listen()
                elif self._voice and self._voice.get_mode() == self._voice.WAKE_LOOP:
                    # Voice might have just come online - try to put it in active listen
                    print("[MONITOR] re-entering ACTIVE_LISTEN", flush=True)
                    self._voice.set_mode(self._voice.ACTIVE_LISTEN)

                time.sleep(0.4)

            except Exception as e:
                print(f"[MANAGER] monitor error: {e}", flush=True)
                time.sleep(1)

    def _read_gaze(self):
        try:
            with open(self._signal_file, encoding="utf-8") as signal:
                payload = json.load(signal)
            return payload.get("gaze", "center")
        except (OSError, ValueError, TypeError):
            return "center"

    def _set_gaze(self, gaze):
        if gaze not in ("center", "left", "right"):
            gaze = "center"
        head = self._head or (getattr(self._app, "head", None) if self._app else None)
        if head is None or not hasattr(head, "set_gaze"):
            return
        if gaze == self._last_gaze:
            return
        self._last_gaze = gaze
        head.set_gaze(gaze)
        print(f"[MONITOR] avatar gaze -> {gaze}", flush=True)

    # ------------------------------------------------------------------ #
    # What happens when we see a fresh signal
    # ------------------------------------------------------------------ #
    def _greet_and_listen(self):
        """Speak a greeting and kick the voice engine into active command mode."""
        print("[HANDLE] _greet_and_listen entered", flush=True)

        # If we aren't bound to tts yet, just remember that we saw a person.
        if not (self._app and self._tts):
            print("[HANDLE] TTS not ready yet – storing detection", flush=True)
            self._pending_detection = True
            return

        # Don't speak if TTS is already busy (e.g., mid-sentence)
        if self._tts.busy.is_set():
            print("[HANDLE] TTS is busy – skipping", flush=True)
            return

        greeting = ""
        print(f"[HANDLE] Speaking: {greeting}", flush=True)

        def on_end():
            try:
                # Voice might not be ready yet (no Listen button clicked).
                # The monitor loop will set ACTIVE_LISTEN once voice appears.
                if self._voice is not None:
                    self._voice.set_mode(self._voice.ACTIVE_LISTEN)
                    self._voice._call("on_wake_word", "[manager] person detected")
                    print("[HANDLE] wake-word triggered, ACTIVE_LISTEN engaged", flush=True)
                else:
                    print("[HANDLE] voice not ready yet, will set ACTIVE_LISTEN later", flush=True)
            except Exception as e:
                print(f"[HANDLE] wake-trigger error: {e}", flush=True)

        try:
            self._tts.speak(greeting, on_end=on_end)
            print("[HANDLE] TTS speak call succeeded", flush=True)
        except Exception as e:
            print(f"[HANDLE] TTS speak error: {e}", flush=True)
            with self._lock:
                self._greeted = False
                self._pending_detection = True

    # ------------------------------------------------------------------ #
    # Clean-up
    # ------------------------------------------------------------------ #
    def shutdown(self):
        """Called when the plugin is unloaded or the app exits."""
        print("[MANAGER] Shutdown requested", flush=True)
        self._running = False
        if self._detector_proc:
            try:
                self._detector_proc.terminate()
                self._detector_proc.wait(timeout=2)
            except Exception:
                try:
                    self._detector_proc.kill()
                except Exception:
                    pass
            self._detector_proc = None
        if os.path.exists(self._signal_file):
            try:
                os.remove(self._signal_file)
            except Exception:
                pass
        self._set_gaze("center")


# ---------------------------------------------------------------------- #
# Optional: allow running the file directly for a quick manual test
# ---------------------------------------------------------------------- #
if __name__ == "__main__":
    mp = ManagerPlugin()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mp.shutdown()
