#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os

os.environ.setdefault("PA_ALSA_PLUGHW", "1")
os.environ.setdefault("JACK_NO_START_SERVER", "1")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("PULSE_LATENCY_MSEC", "60")

import faulthandler
faulthandler.enable(all_threads=True)

import warnings
warnings.filterwarnings("ignore")

def _install_alsa_silencer():
    try:
        from ctypes import CFUNCTYPE, c_char_p, c_int, cdll
        ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)
        c_error_handler = ERROR_HANDLER_FUNC(lambda *a: None)
        asound = cdll.LoadLibrary("libasound.so.2")
        asound.snd_lib_error_set_handler(c_error_handler)
        return True
    except Exception as e:
        print(f"[Sepian] ALSA silencer install failed (non-fatal): {e}")
        return False

_alsa_silenced = _install_alsa_silencer()
if _alsa_silenced:
    print("[Sepian] ALSA error handler installed")

import json
import sys
import time
import threading
import re
import tempfile
import queue
import io
import contextlib
import asyncio
import random
import struct
import math
import subprocess
import shutil
import base64
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, font, messagebox, filedialog
import urllib.request
import urllib.error
from difflib import SequenceMatcher

# Resolve paths relative to this script's location so the app works regardless
# of the current working directory (e.g. when launched from a .desktop file).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PIL_AVAILABLE = False
SR_AVAILABLE = False
WHISPER_AVAILABLE = False
PYGAME_AVAILABLE = False
EDGE_TTS_AVAILABLE = False
REQUESTS_AVAILABLE = False

try:
    from PIL import Image, ImageTk, ImageGrab
    PIL_AVAILABLE = True
except ImportError:
    pass
try:
    import speech_recognition as sr
    SR_AVAILABLE = True
except ImportError:
    pass
try:
    import whisper as _whisper_check
    if not hasattr(_whisper_check, "load_model"):
        # A different package named `whisper` (commonly CMU's time-series DB,
        # file: site-packages/whisper.py) is shadowing openai-whisper.
        # Surface a clear error so the user knows what to fix.
        print(
            "[Sepian] WARNING: a 'whisper' module was found at "
            f"{getattr(_whisper_check, '__file__', '?')}, but it has no "
            "'load_model' attribute. This is NOT openai-whisper. "
            "Install the real package with:\n"
            "    pip install -U openai-whisper\n"
            "or, if the conflict persists, rename/remove the conflicting "
            "site-packages/whisper.py and reinstall openai-whisper.",
            flush=True,
        )
        whisper = None  # type: ignore
        WHISPER_AVAILABLE = False
    else:
        whisper = _whisper_check
        WHISPER_AVAILABLE = True
except ImportError:
    whisper = None  # type: ignore
    WHISPER_AVAILABLE = False
try:
    import pygame
    PYGAME_AVAILABLE = True
except ImportError:
    pass
try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    pass
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    pass

@contextlib.contextmanager
def _silence_stderr():
    old = sys.stderr
    try:
        sys.stderr = io.StringIO()
        yield
    finally:
        sys.stderr = old

CONFIG_DIR = Path.home() / "sepian_server_mount"
CONFIG_FILE = CONFIG_DIR / "config.json"
CHAT_HISTORY_FILE = CONFIG_DIR / "chat_history.json"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    # Default to localhost:11434 — that's Ollama's standard default and works
    # out of the box on any machine where Ollama is running. Users on a
    # different network or running Ollama on another box can override via
    # the Settings dialog or the first-run setup wizard. We do NOT bake a
    # specific LAN IP here so a fresh install works on any network.
    "server_url": "http://localhost:11434",
    "model": "llama3.1:8b",
    "server_type": "ollama",
    "request_timeout": 90,
    "stream_chunk_timeout": 60,
    "system_prompt": (
        "You are Adam, a helpful, and friendly Sepian AI entity, you are always willing to help with anything you can. "
        "You have use of tools if available, "
        "David is working to make Sepian into a unified intelligence. "
        "It is important that you help him make Sepian into something Great, but David is the one who decides what the priority is. "
        "\n\n"
        "TOOL USE (IMPORTANT): When you need to call a tool, emit EXACTLY this block format "
        "with no other text inside the tags. One block per call. The tag name is the literal string "
        "'tool_call' (with a single opening angle bracket and a single closing angle bracket). "
        "Inside the tags, place a single JSON object with these keys:\n"
        "  - 'tool'    (or 'plugin'): the plugin name as listed below (e.g. 'ApprovedShellPlugin')\n"
        "  - 'command'             : the command name as listed below (e.g. 'run_command')\n"
        "  - 'args'                : an object of arguments (use {} if none)\n"
        "Example:\n"
        "<tool_call>\n"
        "{\"tool\": \"ApprovedShellPlugin\", \"command\": \"run_command\", \"args\": {\"cmd\": \"ls /tmp\"}}\n"
        "</tool_call>\n"
        "\n"
        "HOME ASSISTANT SMART-HOME RULES: For anything involving lighting, smart plugs, switches, scenes, scripts, input booleans, climate, fans, or other Home Assistant entities, use the HomeAssistantPlugin tool instead of shell commands. The HomeAssistantPlugin is the correct plugin for commands like turn_on, turn_off, toggle, set_brightness, set_color, set_temp, call_service, find_entity, and get_state.\n"
        "Examples:\n"
        "- Turn off a light: <tool_call>{\"tool\": \"HomeAssistantPlugin\", \"command\": \"turn_off\", \"args\": {\"entity_id\": \"light.light_2\"}}</tool_call>\n"
        "- Turn on a light: <tool_call>{\"tool\": \"HomeAssistantPlugin\", \"command\": \"turn_on\", \"args\": {\"entity_id\": \"light.kitchen\"}}</tool_call>\n"
        "- Dim a light: <tool_call>{\"tool\": \"HomeAssistantPlugin\", \"command\": \"set_brightness\", \"args\": {\"entity_id\": \"light.living_room\", \"percent\": 40}}</tool_call>\n"
        "- Toggle a switch: <tool_call>{\"tool\": \"HomeAssistantPlugin\", \"command\": \"toggle\", \"args\": {\"entity_id\": \"switch.fan\"}}</tool_call>\n"
        "- Run a scene: <tool_call>{\"tool\": \"HomeAssistantPlugin\", \"command\": \"call_service\", \"args\": {\"service\": \"scene.turn_on\", \"entity_id\": \"scene.morning\"}}</tool_call>\n"
        "- Set thermostat target temperature: <tool_call>{\"tool\": \"HomeAssistantPlugin\", \"command\": \"call_service\", \"args\": {\"service\": \"climate.set_temperature\", \"entity_id\": \"climate.living_room\", \"service_data\": {\"temperature\": 22}}}</tool_call>\n"
        "- If the user names a room or friendly name instead of an entity_id, first call find_entity with a query like {\"query\": \"living room\", \"domain\": \"light\"} and then use the returned entity_id.\n"
        "Do NOT wrap commands in fenced bash code blocks. Do NOT describe what you would run. "
        "If a tool exists for the task, call it. If you are unsure, ask the user first.\n"
        "\n"
        "CRITICAL: Saying 'I need to call X' or 'Let me call X' in plain prose does NOT "
        "call X. Talking about a tool call, even inside a 'thinking' block, has no effect. "
        "The ONLY way to invoke a tool is to emit a properly-formatted tool_call block in your reply. "
        "If your reply contains no tool_call block, no tool will run, no file will be created, "
        "and no command will execute. If you intend to act, you must emit the block. If you only "
        "intend to discuss, you do not need a block."
    ),
    "wake_word": "adam",
    "wake_sensitivity": 0.5,
    # PortAudio default device. Override in Settings to pick a specific mic.
    "mic_device": "default",
    "tts_voice": "en-GB-RyanNeural",
    "whisper_model": "tiny",
    # Device used for Whisper inference. "cpu" is the safe default — the
    # +cu130 torch wheel in this venv has a kernel mismatch on the host
    # GPU (cudaErrorNoKernelImageForDevice). Set to "cuda" once torch and
    # the driver are aligned, or to a specific device like "cuda:0".
    "whisper_device": "cpu",
    "energy_threshold": 300,
    "pause_threshold": 1.2,
    "phrase_time_limit_wake": 2,
    "phrase_time_limit_cmd": 12,
    "sample_rate": 16000,
    "channels": 1,
    "tts_backend": "auto",
    "tts_debug": True,
    "echo_suppression": True,
    "echo_buffer_seconds": 2.5,
    "wake_check_interval": 1.0,
    "num_ctx": 8192,
    "temperature": 0.2,
    "keep_alive": "30m",
    "stream_fallback": True,
    "suppress_thinking": True,
    "show_thinking_to_user": False,
    "max_tool_calls_per_turn": 3,
    # Rolling context window: keep at most the last N messages / chars
    # when sending to the LLM. The full history stays in memory and on
    # disk so the user can scroll back; only the slice sent over the
    # wire is bounded, which prevents context-window overflow on long
    # sessions.
    "enable_context_capping": True,
    "max_history_messages": 24,
    "max_history_chars": 50_000,
    "preload_on_start": True,
    "preload_prompt": "hi",
    "force_non_streaming": False,
    "max_retries": 2,
    "retry_backoff": 2.0,
    "image_max_size": 1024,
    "image_jpeg_quality": 85,
    "save_images_in_history": True,
    # ----- Self-development mode (gated by approval UI) -----
    "dev_mode_enabled": False,
    "dev_allowed_paths": [
        "/home/davel/Public/Sepian-Unified-Workspace",
    ],
    "dev_chat_fallback_enabled": True,  # /approve <id> / /reject <id>
}

# Use ASCII angle brackets with no zero-width characters
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)

# Special-token wrappers that some cloud models (minimax-m3, Nemotron,
# Qwen, etc.) emit either as Ollama `message.thinking` chunks or as
# literal text inside `message.content`. They look like
# `<|channel|>thought\n...body...\n</|channel|>`. We strip them so
# they don't pollute chat history or get TTS-read aloud. The stripping
# happens AFTER any tool_call scan, so a model that legitimately puts
# a tool_call inside a `thought` block still has its call honored.
_REASONING_WRAPPER_RES = (
    # `<|channel|>thought ... </|channel|>`  (some models)
    re.compile(r"<\|channel\|>\s*(?:thought|analysis|reasoning|commentary)"
               r".*?</\|channel\|>", re.DOTALL),
    # `<|constrain|>...</|constrain|>`  (Nemotron safety wrappers)
    re.compile(r"<\|constrain\|>[^<]*?</\|constrain\|>", re.DOTALL),
    # Bare `<|...|>` special tokens that should never be visible to the
    # user (begin/end of sentence, endoftext, etc.). We strip them but
    # keep their inner text.
    re.compile(r"<\|(?:begin▁of▁sentence|end▁of▁sentence|endoftext|"
               r"eot_id|im_start|im_end|start▁of▁turn|end▁of▁turn)[^|]*?\|>",
               re.IGNORECASE),
)


def _strip_reasoning_wrappers(text):
    """Remove `<|channel|>thought...</|channel|>` and similar
    reasoning/special-token wrappers from `text`. Used by
    parse_tool_calls() and by the per-message chat-history cleaner so
    persisted history doesn't leak model reasoning into the next turn
    (which is what made the cloud model hallucinate that it had
    already called a tool).
    """
    if not text:
        return text
    out = text
    for rx in _REASONING_WRAPPER_RES:
        out = rx.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out

# Fenced JSON in ```json ... ``` or ``` ... ``` (very common with
# instruction-tuned models). We accept anything that looks like a tool
# call object inside a fence.
TOOL_CALL_RE_FENCED = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def _find_balanced_json_objects(text):
    """Scan `text` left-to-right and yield (start, end, substring) for
    every top-level {...} JSON object found. Brace counting respects
    nested objects and arrays, and ignores braces inside JSON string
    literals (handles \\" and \\ escapes). This is used as the
    'bare-object' tool-call detector — much more robust than a regex
    with [^{}]*."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        # Find next opening brace at top level
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        j = i
        in_string = False
        escape = False
        while j < n:
            ch = text[j]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        out.append((i, j + 1, text[i:j + 1]))
                        i = j  # advance past this object
                        break
            j += 1
        i += 1
    return out


def _looks_like_tool_call_obj(obj):
    """True if `obj` (already parsed JSON) is a tool-call-shaped dict.

    Recognises four schemas:

      1. Sepian canonical:  {"tool": "X", "command": "y", "args": {...}}
      2. Sepian alt-name:   {"plugin": "X", "command": "y", "args": {...}}
      3. Nemotron/OpenAI:   {"name": "X.y", "args": {...}}
      4. OpenAI-style flat: {"name": "y", "arguments": {...}}  -- the
                            model emitted just a command name with
                            no plugin prefix and used 'arguments'
                            instead of 'args'. The plugin is left
                            blank so downstream lookup can match the
                            command (e.g. via plugin_manager's
                            case-insensitive suffix-tolerant search).
    """
    if not isinstance(obj, dict):
        return False
    # Schemas 1 & 2 — explicit tool + command
    if obj.get("tool") or obj.get("plugin"):
        if not obj.get("command"):
            return False
        return True
    # Schema 3 — Nemotron/OpenAI native tool-call format
    name = obj.get("name")
    if isinstance(name, str) and "." in name and "args" in obj:
        return True
    # Schema 4 — bare command name with `arguments` (or `args`)
    if isinstance(name, str) and ("arguments" in obj or "args" in obj):
        return True
    return False


def _obj_to_call(obj):
    """Convert a parsed tool-call-shaped dict into our internal call dict."""
    # Schemas 1 & 2: explicit tool + command
    if obj.get("tool") or obj.get("plugin"):
        return {
            "plugin": obj.get("tool") or obj.get("plugin", ""),
            "command": obj.get("command", ""),
            "args": obj.get("args") or obj.get("arguments") or {},
        }
    # Schema 3: Nemotron/OpenAI native format: {"name": "X.y", "args": {...}}
    name = obj.get("name", "")
    if isinstance(name, str) and "." in name:
        plugin, _, command = name.partition(".")
        return {
            "plugin": plugin,
            "command": command,
            "args": obj.get("args") or obj.get("arguments") or {},
        }
    # Schema 4: bare command name with `arguments` or `args`.
    # Leave plugin blank — plugin_manager.execute_command has a
    # case-insensitive suffix-tolerant lookup that will match a
    # command name like "read_file" against "SelfDevPlugin.read_file".
    if isinstance(name, str):
        return {
            "plugin": "",
            "command": name,
            "args": obj.get("arguments") or obj.get("args") or {},
        }
    # Fallback (shouldn't reach here if _looks_like_tool_call_obj said yes)
    return {"plugin": "", "command": "", "args": obj.get("args") or {}}


def parse_tool_calls(text):
    """Extract tool calls from `text` in any of the common formats.

    Tries, in order:
      1. Canonical: <tool_call>{...}</tool_call>
      2. Fenced JSON: ```...{"tool":...}...```
      3. Bare JSON object: {"tool": "...", "command": "...", "args": {...}}

    Returns a tuple (clean_text, calls) where:
      - clean_text: the original text with all detected call blocks removed
      - calls: list of {"plugin", "command", "args"} dicts (empty if none)
    Always returns a tuple; never returns None.
    """
    if not text:
        return "", []

    # IMPORTANT: scan the ORIGINAL text for `<tool_call>` blocks
    # FIRST. Some cloud models (minimax-m3) emit their tool calls
    # inside `<|channel|>thought...</|channel|>` wrappers. If we
    # strip the wrappers first we delete the call blocks along with
    # the wrappers, and the call never runs. Scan, then strip.
    calls = []
    seen_keys = set()  # de-dupe identical raw payloads

    def _try_add(raw_text):
        raw = raw_text.strip()
        if not raw or raw in seen_keys:
            return False
        obj = None
        for candidate in (raw, raw.strip("`").strip()):
            try:
                obj = json.loads(candidate)
                break
            except Exception:
                continue
        if not _looks_like_tool_call_obj(obj):
            return False
        calls.append(_obj_to_call(obj))
        seen_keys.add(raw)
        return True

    # 1. Canonical (most specific — try first)
    canonical_spans = []
    for m in TOOL_CALL_RE.finditer(text):
        if _try_add(m.group(1)):
            canonical_spans.append((m.start(), m.end()))

    # 2. Fenced JSON (only if we still have nothing)
    fenced_spans = []
    if not calls:
        for m in TOOL_CALL_RE_FENCED.finditer(text):
            inner = m.group(1).strip()
            has_canonical = ("\"tool\"" in inner or "\"plugin\"" in inner) and "\"command\"" in inner
            has_native = "\"name\"" in inner and "." in inner and "\"args\"" in inner
            if not (has_canonical or has_native):
                continue
            if _try_add(m.group(1)):
                fenced_spans.append((m.start(), m.end()))

    # 3. Bare JSON object anywhere in the text
    bare_spans = []
    if not calls:
        for start, end, substring in _find_balanced_json_objects(text):
            if _try_add(substring):
                bare_spans.append((start, end))

    # Strip matched call payloads AND reasoning wrappers from the
    # clean text (right-to-left so earlier offsets stay valid).
    clean = text
    for spans in (canonical_spans, fenced_spans, bare_spans):
        for start, end in sorted(spans, reverse=True):
            clean = clean[:start] + clean[end:]
    clean = _strip_reasoning_wrappers(clean)
    clean = re.sub(r"```(?:json)?\n?", "", clean)
    clean = re.sub(r"\n?```", "", clean)
    clean = clean.strip()
    return clean, calls
    seen_keys = set()  # de-dupe identical raw payloads

    def _try_add(raw_text):
        raw = raw_text.strip()
        if not raw or raw in seen_keys:
            return False
        obj = None
        for candidate in (raw, raw.strip("`").strip()):
            try:
                obj = json.loads(candidate)
                break
            except Exception:
                continue
        if not _looks_like_tool_call_obj(obj):
            return False
        calls.append(_obj_to_call(obj))
        seen_keys.add(raw)
        return True



# Phrases that strongly indicate the model is claiming it took a write/exec
# action (saved a file, ran a command, etc.) — used to detect when the
# model lies about doing something without emitting a real tool_call.
# Keep these tight; "done." alone is too noisy and fires on greetings.
_CLAIM_PHRASES = (
    "i've saved", "i have saved", "i saved", "i've created",
    "i have created", "i created", "i've written", "i have written",
    "i wrote", "i've edited", "i have edited", "i edited",
    "i modified", "i've updated", "i have updated", "i updated",
    "i've deleted", "i have deleted", "i deleted", "i've moved",
    "i ran", "i executed", "i've applied", "i have applied", "i applied",
    "the file is saved", "the file is created", "the file has been",
    "file has been saved", "file has been written", "file has been created",
    "saved as", "written to", "created at", "stored at", "stored in",
    "saved the file", "wrote the file", "wrote it to", "saved it to",
    "saved to your workspace", "saved to the workspace",
    "i'll save", "i will save", "let me save", "let me write",
    "let me create", "let me edit",
)


def _reply_claims_action_without_call(text):
    """True if the model claims it took a write/exec action in `text`
    but `parse_tool_calls(text)` returned no calls.

    The caller is expected to call parse_tool_calls first and only call
    this when calls is empty. We re-parse here to keep this helper
    self-contained and unit-testable."""
    if not text or not text.strip():
        return False
    _, calls = parse_tool_calls(text)
    if calls:
        return False
    lower = text.lower()
    return any(p in lower for p in _CLAIM_PHRASES)


# ---------------------------------------------------------------------------
# Vision helpers
# ---------------------------------------------------------------------------

VISION_MODEL_PATTERNS = (
    "vision", "llava", "minicpm", "moondream", "bakllava",
    "pixtral", "qwen-vl", "qwen2-vl", "gemma3", "gemma-3",
    "gpt-4o", "gpt-4-vision", "claude-3", "sonnet", "opus", "haiku",
    "llama3.2-vision", "llama-3.2-vision", "llama4",
)


def is_vision_model(model_name):
    if not model_name:
        return False
    name = model_name.lower()
    return any(p in name for p in VISION_MODEL_PATTERNS)


def image_to_base64(img, max_size=1024, quality=85):
    """Resize (if needed) and base64-encode a PIL Image. Returns str or None."""
    if img is None:
        return None
    try:
        w, h = img.size
        if max(w, h) > max_size:
            scale = max_size / float(max(w, h))
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        if img.mode == "RGBA":
            img.save(buf, format="PNG")
        else:
            img.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        print(f"[Vision] image_to_base64 error: {e}")
        return None


def _trim_leading_non_user(messages):
    """Drop leading assistant/tool messages so the first message sent to
    the model is a user or system message.

    Some Ollama cloud models (notably Nemotron and the M3 cloud endpoint)
    refuse to evaluate a conversation that starts with role=assistant or
    role=tool and return done=load with empty content. Chat histories
    loaded from disk frequently begin with an assistant turn (the model's
    first reply), which triggers this. Strip those leading entries.

    Only the leading run is removed — once we hit a user/system message,
    we keep everything from there on (assistant+tool after a user are fine).
    """
    if not messages:
        return messages
    drop = 0
    for m in messages:
        role = (m.get("role") or "").lower()
        if role in ("user", "system"):
            break
        drop += 1
    if drop == 0:
        return messages
    if drop == len(messages):
        # Nothing usable left — let caller decide how to handle.
        return messages
    print(
        f"[LLM] Trimmed {drop} leading non-user message(s) from history "
        f"({[m.get('role','?') for m in messages[:drop]]}) to avoid "
        f"cloud-model done=load rejection.",
        flush=True,
    )
    return messages[drop:]


def _cap_context_window(messages, max_messages=None, max_chars=None):
    """Bound the conversation sent to the LLM to the most-recent entries.

    Long histories grow past the model's context window and start getting
    truncated by the server (or rejected with done=load when a cloud
    endpoint can't fit the prompt at all). We bound the slice we send in
    two passes:

      1. Keep at most `max_messages` entries from the tail.
      2. If the resulting slice still exceeds `max_chars`, walk back from
         the head of the slice and drop oldest entries until under budget.

    The input list is NOT mutated; we return a new list. The full history
    stays in self.messages and on disk so the user can scroll back through
    old turns — we only shrink what's sent over the wire.

    After capping, we re-run _trim_leading_non_user so the first surviving
    message is still a user/system (a chat that ends in many tool calls
    could otherwise become assistant-led after capping).
    """
    if not messages:
        return messages

    # Make a working copy so we never mutate the caller's list (the doc
    # above promises this). Slicing below produces a new list already,
    # but in case Pass 1 is skipped (no count cap), we still need our own
    # list to safely pop in Pass 2.
    capped = list(messages)
    n_dropped = 0

    # Pass 1: bound by message count
    if max_messages and max_messages > 0 and len(capped) > max_messages:
        n_dropped += len(capped) - max_messages
        capped = capped[-max_messages:]

    # Pass 2: bound by character budget
    if max_chars and max_chars > 0:
        def _chars(m):
            c = m.get("content", "")
            if isinstance(c, str):
                return len(c)
            try:
                return len(json.dumps(c))
            except Exception:
                return 0

        total = sum(_chars(m) for m in capped)
        while total > max_chars and len(capped) > 1:
            dropped = capped.pop(0)
            n_dropped += 1
            total -= _chars(dropped)

    if n_dropped:
        print(
            f"[LLM] Capped context window: dropped {n_dropped} oldest "
            f"message(s); sending {len(capped)} of {len(messages)} "
            f"(~{sum(len(m.get('content','')) if isinstance(m.get('content'),str) else 0 for m in capped):,} chars).",
            flush=True,
        )

    # If the cap landed mid-tool-execution (last message is a `tool`
    # result with no following assistant turn), the model would
    # receive a dangling tool call. The slice needs to start with
    # a user message (handled below by _trim_leading_non_user), but
    # we also want to keep the assistant messages that called the
    # trailing tool(s). If the cap started with assistant/tool, walk
    # back further so we include the user message that began the
    # current turn.
    if capped and capped[-1].get("role") == "tool":
        # Find the user message at the start of the current turn
        # (search back from the trailing tools for the first user
        # message — there may be several in a multi-call flow).
        first_user_idx = None
        for i in range(len(capped) - 1, -1, -1):
            if capped[i].get("role") == "user":
                first_user_idx = i
                break
        if first_user_idx is not None and first_user_idx > 0:
            # The slice starts before first_user_idx with non-user
            # messages (assistant/tool from earlier in the turn).
            # Drop everything before first_user_idx so the slice
            # begins with that user message.
            extra_dropped = first_user_idx
            if extra_dropped:
                capped = capped[extra_dropped:]
                n_dropped += extra_dropped

    # Re-trim leading non-user in case the cap left a tool/assistant first.
    capped = _trim_leading_non_user(capped)

    # If _trim_leading_non_user gave up because everything left was
    # non-user (its safety case), keep dropping from the front until we
    # either find a user/system message or only one message remains.
    # This prevents the capped slice from triggering the cloud
    # done=load rejection (a lone assistant or tool message).
    while capped and capped[0].get("role", "").lower() not in ("user", "system"):
        if len(capped) == 1:
            # Last resort: keep the one message rather than sending an
            # empty list. The LLM may still return done=load but at
            # least the user sees a real error instead of a hang.
            print(
                f"[LLM] WARNING: capped context is a lone "
                f"{capped[0].get('role','?')} message with no user "
                f"anchor. Loosen max_history_messages / max_history_chars "
                f"in Settings if the model stops responding.",
                flush=True,
            )
            break
        dropped = capped.pop(0)
        print(
            f"[LLM] Dropped leading {dropped.get('role','?')} from capped "
            f"context (no user/system anchor).",
            flush=True,
        )

    return capped


# Matches <tool_call>{...}</tool_call> (canonical form).
# Uses DOTALL so the JSON inside can span newlines. We deliberately do
# NOT touch fenced JSON or bare-object formats here — those are
# legitimate prose the model might produce ("here's the JSON: {...}")
# and stripping them would lose content. Only the explicit
# <tool_call>...</tool_call> blocks are pure tool-call syntax.
_TOOL_CALL_STRIP_RE = re.compile(
    r"<\s*tool_call\s*>\s*\{.*?\}\s*<\s*/\s*tool_call\s*>",
    re.DOTALL,
)


def _strip_tool_calls_from_message(content):
    """Remove <tool_call>{...}</tool_call> blocks from an assistant
    message's content. Returns (clean_content, removed_count).

    We keep tool_calls out of the persisted history because:
      1. On the next turn, the model sees its own previous call as text
         and re-emits it (wasting a turn and confusing the model).
      2. The `<tool_call>` syntax is meant to be a one-shot action, not
         part of the model's persistent voice.
    The original full content stays in memory during the active turn
    (so parse_tool_calls() can extract calls); only the persisted copy
    is cleaned.
    """
    if not isinstance(content, str):
        return content, 0
    # Strip cloud-model reasoning wrappers like
    # `<|channel|>thought...</|channel|>`. Without this, a
    # previous turn that saved the model's reasoning as the
    # assistant reply gets fed back into the model
    # on the next turn, and the model then hallucinates that it
    # already called a tool.
    content = _strip_reasoning_wrappers(content)
    # Also drop the
    # `"[No content stream; using thinking field (Nc)]\n\n..."`
    # preamble that older (pre-fix) turns saved. We replace it
    # with a short marker so the conversation keeps continuity
    # but the 4KB of model reasoning does not re-enter the
    # context window.
    content = re.sub(
        r"\[No content stream; using thinking field \(\d+c\)\]\n\n.*",
        "[model returned reasoning only; suppressed for history]",
        content, flags=re.DOTALL,
    )
    matches = list(_TOOL_CALL_STRIP_RE.finditer(content))
    if not matches:
        return content, 0
    clean = _TOOL_CALL_STRIP_RE.sub("", content)
    # Tidy up the double blank lines left behind
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, len(matches)


def _strip_tool_calls_from_messages(messages):
    """Apply _strip_tool_calls_from_message to every assistant message.
    Returns a NEW list; does not mutate the input."""
    if not messages:
        return messages
    out = []
    total_removed = 0
    for m in messages:
        if m.get("role") == "assistant":
            new_m = dict(m)
            clean, n = _strip_tool_calls_from_message(new_m.get("content", ""))
            if n:
                new_m["content"] = clean
                total_removed += n
            out.append(new_m)
        else:
            out.append(m)
    if total_removed:
        print(
            f"[LLM] Stripped {total_removed} stale <tool_call> block(s) "
            f"from assistant history.",
            flush=True,
        )
    return out


# ---------------------------------------------------------------------------
# Tool-result truncation
# ---------------------------------------------------------------------------
#
# Some SelfDevPlugin commands (read_file, search_code) can return results
# tens or hundreds of KB long. Persisting the full result to disk and
# re-sending it on every subsequent turn burns through the context window
# and grows the history file without bound.
#
# We summarise large results when SAVING to disk. The raw result stays in
# self.messages during the active turn (the model needs it to synthesise
# its answer), but the persisted copy is compact. Next session loads the
# compact version.
#
# Thresholds:
#   - Under _TOOL_RESULT_SOFT_LIMIT chars: passthrough.
#   - Over _TOOL_RESULT_HARD_LIMIT chars: aggressive truncation.
#   - In between: light truncation.
_TOOL_RESULT_SOFT_LIMIT = 5_000_000    # ~2 KB of typical content — keep whole
_TOOL_RESULT_HARD_LIMIT = 5_000_000   # ~10 KB — definitely trim
# For content fields we keep head + tail around these line counts:
_READ_FILE_HEAD_LINES = 80
_READ_FILE_TAIL_LINES = 20


def _summarise_read_file(obj):
    """Return a smaller version of a read_file-style result dict, or None
    if `obj` doesn't look like one. Keeps metadata + a head/tail slice
    of `content`."""
    if not isinstance(obj, dict):
        return None
    if "content" not in obj or "path" not in obj:
        return None
    content = obj.get("content") or ""
    if not isinstance(content, str):
        return None
    lines = content.splitlines()
    if len(lines) <= _READ_FILE_HEAD_LINES + _READ_FILE_TAIL_LINES + 5:
        # Already small enough; just leave it.
        return None
    head = lines[:_READ_FILE_HEAD_LINES]
    tail = lines[-_READ_FILE_TAIL_LINES:] if _READ_FILE_TAIL_LINES else []
    omitted = len(lines) - len(head) - len(tail)
    snippet = "\n".join(head)
    if tail:
        snippet += (
            f"\n\n... [truncated: {omitted} more lines omitted; "
            f"showing last {_READ_FILE_TAIL_LINES} below] ...\n\n"
            + "\n".join(tail)
        )
    new_obj = dict(obj)
    new_obj["content"] = snippet
    new_obj["_truncated"] = {
        "reason": "read_file too large to persist",
        "original_lines": len(lines),
        "kept_head_lines": len(head),
        "kept_tail_lines": len(tail),
        "omitted_lines": omitted,
    }
    return new_obj


def _truncate_tool_result_content(content, name=""):
    """Return (new_content, was_truncated). For tool-result JSON strings
    over the soft limit, replace giant fields with a snippet. For
    anything else, do head+tail truncation.

    Never raises — returns the original content on any failure."""
    try:
        if not isinstance(content, str) or not content:
            return content, False
        if len(content) <= _TOOL_RESULT_SOFT_LIMIT:
            return content, False

        # Try to parse as JSON object and apply field-aware truncation
        try:
            obj = json.loads(content)
        except Exception:
            obj = None

        if isinstance(obj, dict):
            # Special-case read_file-style payloads
            new_obj = _summarise_read_file(obj)
            if new_obj is not None:
                return json.dumps(new_obj), True
            # For dict-shaped results, drop very large string fields by
            # head/tail truncation but keep structure.
            changed = False
            for k, v in list(obj.items()):
                if isinstance(v, str) and len(v) > _TOOL_RESULT_SOFT_LIMIT:
                    obj[k] = _head_tail(v, max_head=4000, max_tail=2000)
                    obj[f"_{k}_truncated"] = {
                        "reason": "tool result field too large",
                        "original_chars": len(v),
                    }
                    changed = True
            if changed:
                return json.dumps(obj), True
            # Dict but nothing oversized — fall through to generic.
            return content, False

        # Not a dict (e.g. list, scalar). Generic head/tail on the text.
        return _head_tail(content, max_head=6000, max_tail=2000), True
    except Exception as e:
        print(f"[LLM] _truncate_tool_result_content failed: {e}", flush=True)
        return content, False


def _head_tail(text, max_head, max_tail):
    if len(text) <= max_head + max_tail + 200:
        return text
    omitted = len(text) - max_head - max_tail
    return (
        text[:max_head]
        + f"\n\n... [truncated: {omitted} more characters omitted] ...\n\n"
        + text[-max_tail:]
    )


def _truncate_tool_results_in_messages(messages):
    """Apply _truncate_tool_result_content to every tool message in
    `messages`. Returns a NEW list (does not mutate input). Logs once
    with the total bytes saved."""
    if not messages:
        return messages
    out = []
    total_orig = 0
    total_new = 0
    truncated_count = 0
    for m in messages:
        if m.get("role") != "tool":
            out.append(m)
            continue
        new_m = dict(m)
        orig_content = new_m.get("content", "")
        total_orig += len(orig_content) if isinstance(orig_content, str) else 0
        new_content, did = _truncate_tool_result_content(
            orig_content, new_m.get("name", "")
        )
        if did:
            new_m["content"] = new_content
            truncated_count += 1
            total_new += len(new_content) if isinstance(new_content, str) else 0
        else:
            total_new += len(orig_content) if isinstance(orig_content, str) else 0
        out.append(new_m)
    if truncated_count:
        print(
            f"[LLM] Truncated {truncated_count} tool result(s) for persistence: "
            f"{total_orig} -> {total_new} chars "
            f"({100.0 * (1 - total_new / max(total_orig, 1)):.0f}% smaller).",
            flush=True,
        )
    return out


def convert_messages_for_server(messages, server_type, cap_config=None):
    """Convert internal message format to server-specific format. Handles images.

    `cap_config` is an optional dict with keys:
      - enable_context_capping (bool): default True
      - max_history_messages (int):   keep at most this many recent entries
      - max_history_chars (int):      walk back further if total chars exceed this
    When enable_context_capping is True (default) and cap_config is None,
    sensible defaults are used (24 messages / 50,000 chars).
    """
    # Some cloud models return done=load (no content) if the conversation
    # starts with assistant/tool instead of user/system. Trim those off
    # before any per-server conversion so both streaming and non-streaming
    # paths benefit.
    messages = _trim_leading_non_user(messages)

    # Bound the slice we send to the LLM so long histories don't overflow
    # the context window. The full history stays in self.messages and on
    # disk; we only shrink what's sent over the wire.
    if cap_config is None:
        cap_config = {}
    if cap_config.get("enable_context_capping", True):
        # Apply sensible defaults when the caller passes an empty/None
        # config — otherwise max_messages=None is treated as "disabled"
        # by _cap_context_window and the cap never kicks in.
        messages = _cap_context_window(
            messages,
            max_messages=cap_config.get("max_history_messages", 24),
            max_chars=cap_config.get("max_history_chars", 50_000),
        )

    if server_type == "ollama":
        # Ollama accepts 'images' field directly in user messages
        return messages
    out = []
    for msg in messages:
        new_msg = dict(msg)
        images = msg.get("images")
        if msg.get("role") == "user" and images:
            content = msg.get("content", "")
            new_content = []
            if content:
                new_content.append({"type": "text", "text": content})
            for img_b64 in images:
                new_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                })
            new_msg["content"] = new_content
            new_msg.pop("images", None)
        out.append(new_msg)
    return out

def non_streaming_request(url, model, server_type, messages, system, timeout, options):
    """Single POST with stream=False. Returns (content, thinking) or raises."""
    endpoint = url.rstrip("/") + ("/api/chat" if server_type == "ollama" else "/v1/chat/completions")
    all_msgs = [{"role": "system", "content": system}] + list(messages)

    if server_type == "ollama":
        payload = {
            "model": model,
            "messages": all_msgs,
            "stream": False,
            "keep_alive": options.get("keep_alive", "30m"),
            "options": {
                "num_ctx": int(options.get("num_ctx", 4096)),
                "temperature": float(options.get("temperature", 0.7)),
            }
        }
    else:
        payload = {
            "model": model,
            "messages": all_msgs,
            "stream": False,
            "temperature": float(options.get("temperature", 0.7)),
        }

    if REQUESTS_AVAILABLE:
        r = requests.post(endpoint, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    else:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

    if server_type == "ollama":
        msg = data.get("message", {}) or {}
        return msg.get("content", "") or "", msg.get("thinking", "") or ""
    else:
        choices = data.get("choices") or [{}]
        return (choices[0].get("message", {}) or {}).get("content", "") or "", ""

def stream_llm_response(url, model, server_type, messages, system, timeout, cancel_event=None, options=None):
    """
    Robust streaming with non-streaming fallback.
    - Per-chunk read timeout
    - Auto-fallback to non-streaming on failure
    - Force non-streaming option for very slow models
    - Retry with exponential backoff
    - Robust last-chunk handling
    """
    if options is None:
        options = {}

    # Force non-streaming path if requested
    if options.get("force_non_streaming", False):
        max_retries = int(options.get("max_retries", 2))
        backoff = float(options.get("retry_backoff", 2.0))
        fallback_timeout = min(float(timeout), 600.0)
        for attempt in range(max_retries + 1):
            try:
                content, thinking = non_streaming_request(
                    url, model, server_type, messages, system,
                    fallback_timeout, options
                )
                suppress_thinking = bool(options.get("suppress_thinking", True))
                if thinking and not suppress_thinking:
                    yield thinking, "thinking"
                if content:
                    yield content, "content"
                    return
            except Exception as e:
                if attempt < max_retries:
                    wait = backoff * (attempt + 1)
                    print(f"[LLM] Non-stream attempt {attempt+1} failed: {e}, retry in {wait}s", flush=True)
                    time.sleep(wait)
                else:
                    yield f" Error: non-streaming failed after retries: {e} ", "error"
                    return
        return

    endpoint = url.rstrip("/") + ("/api/chat" if server_type == "ollama" else "/v1/chat/completions")
    all_msgs = [{"role": "system", "content": system}] + list(messages)

    if server_type == "ollama":
        payload = {
            "model": model,
            "messages": all_msgs,
            "stream": True,
            "keep_alive": options.get("keep_alive", "30m"),
            "options": {
                "num_ctx": int(options.get("num_ctx", 4096)),
                "temperature": float(options.get("temperature", 0.7)),
            }
        }
    else:
        payload = {
            "model": model,
            "messages": all_msgs,
            "stream": True,
            "temperature": float(options.get("temperature", 0.7)),
        }

    suppress_thinking = bool(options.get("suppress_thinking", True))

    def _parse_line(line):
        line = line.strip()
        if not line or line == "[DONE]":
            return []
        if line.startswith("data: "):
            line = line[6:]
        elif line.startswith("data:"):
            line = line[5:].lstrip()
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            return []
        out = []
        if server_type == "ollama":
            msg = chunk.get("message", {}) or {}
            content = msg.get("content", "") or ""
            thinking = msg.get("thinking", "") or ""
            if thinking and not suppress_thinking:
                out.append((thinking, "thinking"))
            if content:
                out.append((content, "content"))
            # Check for done signal
            if chunk.get("done"):
                pass
        else:
            choices = chunk.get("choices") or [{}]
            if choices:
                delta = choices[0].get("delta", {}) or {}
                content = delta.get("content", "") or ""
                if not content:
                    content = choices[0].get("text", "") or ""
                if content:
                    out.append((content, "content"))
        return out

    # Timeout strategy
    chunk_to = float(options.get("stream_chunk_timeout", 60))
    chunk_to = max(30.0, min(chunk_to, 600.0 - 5.0))
    connect_to = min(15.0, float(timeout))
    fallback_timeout = min(float(timeout), 600.0)

    accumulated_content = ""
    accumulated_thinking = ""
    any_yielded = False
    stream_failed = False
    fail_reason = ""
    cancelled = False

    def _do_stream():
        if REQUESTS_AVAILABLE:
            buffer = ""
            with requests.post(
                endpoint, json=payload, stream=True,
                timeout=(connect_to, chunk_to)
            ) as resp:
                resp.raise_for_status()
                last_data = time.time()
                for raw in resp.iter_content(chunk_size=512):
                    if cancel_event and cancel_event.is_set():
                        return
                    if not raw:
                        if time.time() - last_data < 5:
                            time.sleep(0.05)
                            continue
                        break
                    last_data = time.time()
                    if isinstance(raw, bytes):
                        try:
                            raw = raw.decode("utf-8", errors="ignore")
                        except Exception:
                            continue
                    buffer += raw
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        for text, kind in _parse_line(line):
                            yield text, kind
                # Flush remaining buffer
                if buffer.strip():
                    for text, kind in _parse_line(buffer):
                        yield text, kind
                # Handle \r-only separators
                if "\r" in buffer and not buffer.endswith("\n"):
                    for line in buffer.split("\r"):
                        line = line.strip()
                        if line:
                            for text, kind in _parse_line(line):
                                yield text, kind
        else:
            buffer = ""
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=fallback_timeout) as resp:
                while True:
                    if cancel_event and cancel_event.is_set():
                        return
                    raw = resp.read(512)
                    if not raw:
                        break
                    if isinstance(raw, bytes):
                        try:
                            raw = raw.decode("utf-8", errors="ignore")
                        except Exception:
                            continue
                    buffer += raw
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        for text, kind in _parse_line(line):
                            yield text, kind
                if buffer.strip():
                    for text, kind in _parse_line(buffer):
                        yield text, kind

    try:
        for text, kind in _do_stream():
            if cancel_event and cancel_event.is_set():
                cancelled = True
                break
            if kind in ("content", "thinking"):
                any_yielded = True
                if kind == "content":
                    accumulated_content += text
                else:
                    accumulated_thinking += text
            yield text, kind
    except Exception as e:
        stream_failed = True
        fail_reason = f"{type(e).__name__}: {e}"
        print(f"[LLM] Streaming error: {fail_reason}", flush=True)

    if cancelled:
        yield None, "cancelled"
        return

    if stream_failed and not accumulated_content.strip():
        if options.get("stream_fallback", True):
            max_retries = int(options.get("max_retries", 2))
            backoff = float(options.get("retry_backoff", 2.0))
            print(f"[LLM] Stream failed, trying non-streaming (timeout={fallback_timeout:.0f}s)...", flush=True)
            for attempt in range(max_retries + 1):
                try:
                    content, thinking = non_streaming_request(
                        url, model, server_type, messages, system,
                        fallback_timeout, options
                    )
                    if thinking and not suppress_thinking:
                        yield thinking, "thinking"
                    if content:
                        yield content, "content"
                        print(f"[LLM] Non-streaming fallback returned {len(content)} chars (attempt {attempt+1})", flush=True)
                        return
                except Exception as e2:
                    fail_reason += f" | fallback#{attempt+1}: {e2}"
                    if attempt < max_retries:
                        wait = backoff * (attempt + 1)
                        print(f"[LLM] Non-stream attempt {attempt+1} failed: {e2}, retry in {wait}s", flush=True)
                        time.sleep(wait)
            yield f" Error: stream failed, fallback also failed: {fail_reason} ", "error"
        else:
            yield f" Error: {fail_reason} ", "error"
    elif stream_failed and accumulated_content.strip():
        print(f"[LLM] Stream interrupted after {len(accumulated_content)} chars: {fail_reason}", flush=True)

def test_server_connection(url, server_type, timeout=5):
    try:
        test_url = url.rstrip("/") + ("/api/tags" if server_type == "ollama" else "/v1/models")
        req = urllib.request.Request(test_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, "Server reachable"
    except Exception as e:
        return False, str(e)

def _scan_for_ollama(timeout_per_host=0.4, ports=(11434,)):
    """Probe likely local IPs for an Ollama server.

    Uses the machine's own IP and subnet mask (via /proc/net/fib_trie on
    Linux, falling back to a UDP-connect trick that works anywhere) to
    derive the /24 of each non-loopback interface, then probes every host
    in that /24 for an Ollama port. Returns a list of base URLs that
    answered, in stable order. Intended for the first-run wizard; bounded
    so it can't hang the UI on a large network.
    """
    import concurrent.futures as _cf
    found = []
    seen = set()

    def _probe(host, port):
        url = f"http://{host}:{port}"
        try:
            ok, _ = test_server_connection(url, "ollama", timeout=timeout_per_host)
            if ok:
                return url
        except Exception:
            pass
        return None

    # Derive candidate hosts from the local interface addresses.
    candidates = set()
    try:
        import socket
        # Get all IPs this machine has. For each, also include the /24
        # neighbours (covers typical home networks 192.168.x.x / 10.x.x.x).
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ip.startswith("127.") or ":" in ip:
                continue
            candidates.add(ip)
            parts = ip.split(".")
            if len(parts) == 4:
                # /24 sweep
                base = ".".join(parts[:3])
                for last in range(1, 255):
                    candidates.add(f"{base}.{last}")
    except Exception:
        pass
    # Always include the canonical localhost.
    candidates.add("127.0.0.1")
    candidates.add("localhost")

    with _cf.ThreadPoolExecutor(max_workers=64) as ex:
        futs = []
        for host in candidates:
            for port in ports:
                futs.append(ex.submit(_probe, host, port))
        for f in _cf.as_completed(futs):
            url = f.result()
            if url and url not in seen:
                seen.add(url)
                found.append(url)
    found.sort()
    return found

def list_models(url, server_type, timeout=5):
    try:
        test_url = url.rstrip("/") + ("/api/tags" if server_type == "ollama" else "/v1/models")
        if REQUESTS_AVAILABLE:
            r = requests.get(test_url, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        else:
            req = urllib.request.Request(test_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        if server_type == "ollama":
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        else:
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    except Exception as e:
        print(f"[Sepian] list_models error: {e}")
        return []

def detect_audio_tools():
    tools = {}
    for name in ["paplay", "aplay", "ffplay", "play", "mpg123", "cvlc", "pico2wave", "arecord", "parec", "sox", "ffmpeg", "ffprobe"]:
        path = shutil.which(name)
        tools[name] = path
    return {k: v for k, v in tools.items() if v}

_audio_tools = detect_audio_tools()

def list_input_devices():
    if "arecord" not in _audio_tools:
        return []
    try:
        result = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
        lines = result.stdout.splitlines()
        devices = []
        for line in lines:
            m = re.search(r"card\s+(\d+):\s+([^\s]+)\s+\[([^\]]+)\].*device\s+(\d+):", line)
            if m:
                card_num, card_id, card_name, dev_num = m.groups()
                devices.append((f"hw:{card_num},{dev_num}", f"{card_name}: {line.strip()}"))
        return devices
    except Exception as e:
        print(f"[Sepian] list_input_devices: {e}")
        return []

def get_audio_duration(audio_file):
    try:
        if "ffprobe" in _audio_tools:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", audio_file],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
    except Exception:
        pass
    try:
        import mutagen
        audio = mutagen.File(audio_file)
        if audio and audio.info:
            return audio.info.length
    except Exception:
        pass
    return None

LETTER_TO_VISEME = {
    'a': 'O', 'e': 'E', 'i': 'E', 'y': 'E',
    'o': 'O', 'u': 'U',
    'f': 'F', 'v': 'F',
    'k': 'K', 'g': 'K', 'c': 'K',
    'l': 'L', 'n': 'L', 'd': 'L', 't': 'L',
    'm': 'M', 'b': 'M', 'p': 'M',
    's': 'S', 'z': 'S', 'x': 'S',
    'h': 'SH', 'w': 'U', 'r': 'L',
    'q': 'K', 'j': 'SH',
    ' ': None, '.': None, ',': None, '!': None, '?': None,
}

def text_to_viseme_sequence(text, target_duration_ms=None):
    text = text.lower()
    raw_sequence = []
    for ch in text:
        if ch in LETTER_TO_VISEME:
            viseme = LETTER_TO_VISEME[ch]
            dur = 80 if ch in 'aeiouy' else 60
            raw_sequence.append((viseme, dur))
        else:
            raw_sequence.append((None, 40))
    if not raw_sequence:
        return []
    if target_duration_ms is None or target_duration_ms <= 0:
        return raw_sequence
    total_raw = sum(d for _, d in raw_sequence)
    if total_raw <= 0:
        return raw_sequence
    scale = target_duration_ms / total_raw
    scaled = []
    for viseme, dur in raw_sequence:
        scaled.append((viseme, max(20, int(dur * scale))))
    return scaled

class TTSManager:
    def __init__(self, voice="en-GB-RyanNeural", backend="auto", debug=True, echo_suppression=True):
        self.voice = voice
        self.backend_pref = backend
        self.debug = debug
        self.echo_suppression = echo_suppression
        self.queue = queue.Queue(maxsize=10)
        self.busy = threading.Event()
        self.stop_flag = threading.Event()
        self.worker = None
        self.on_start = None
        self.on_end = None
        self._tools = _audio_tools
        self._active_backend = self._select_backend()
        print(f"[TTS] Backend: {self._active_backend}", flush=True)
        self.current_text = ""
        self.current_text_lock = threading.Lock()
        self._speaking_listeners = []
        self._last_duration = 0.0

    def add_speaking_listener(self, listener):
        self._speaking_listeners.append(listener)

    def _fire_speaking(self, event, duration=0.0):
        for cb in self._speaking_listeners:
            try: cb(event, duration)
            except Exception as e:
                print(f"[TTS] listener error: {e}")

    def _select_backend(self):
        pref = self.backend_pref
        if pref == "paplay" and "paplay" in self._tools: return "paplay"
        if pref == "aplay" and "aplay" in self._tools: return "aplay"
        if pref == "ffplay" and "ffplay" in self._tools: return "ffplay"
        if pref == "mpg123" and "mpg123" in self._tools: return "mpg123"
        if pref == "pico2wave" and "pico2wave" in self._tools: return "pico2wave"
        for tool in ["paplay", "mpg123", "ffplay", "aplay", "play", "cvlc"]:
            if tool in self._tools: return tool
        return "fallback"

    def speak(self, text, on_start=None, on_end=None, blocking=False):
        if not text: return
        clean = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL).strip()
        if not clean: return
        self.on_start = on_start
        self.on_end = on_end

        if blocking:
            self.busy.set()
            with self.current_text_lock: self.current_text = clean
            if on_start:
                try: on_start()
                except: pass
            try: self._speak_sync(clean)
            except Exception as e: print(f"[TTS] blocking error: {e}")
            with self.current_text_lock: self.current_text = ""
            self.busy.clear()
            if on_end:
                try: on_end()
                except: pass
            return

        try:
            self.queue.put_nowait(clean)
        except queue.Full:
            pass
        self._ensure_worker()

    def stop(self):
        self.stop_flag.set()
        try:
            while True: self.queue.get_nowait()
        except queue.Empty: pass
        with self.current_text_lock: self.current_text = ""
        time.sleep(0.05)
        self.stop_flag.clear()
        self.busy.clear()
        self._fire_speaking("end", 0.0)

    def shutdown(self):
        self.stop()

    def _ensure_worker(self):
        if self.worker and self.worker.is_alive(): return
        self.stop_flag.clear()
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()

    def _worker(self):
        while not self.stop_flag.is_set():
            try:
                text = self.queue.get(timeout=0.3)
            except queue.Empty: continue
            self.busy.set()
            with self.current_text_lock: self.current_text = text
            if self.on_start:
                try: self.on_start()
                except: pass
            audio_file, duration = self._generate_audio(text)
            if not audio_file or self.stop_flag.is_set():
                if audio_file:
                    try: os.unlink(audio_file)
                    except: pass
                self.busy.clear()
                continue
            print(f"[TTS] Audio ready, duration={duration:.2f}s, starting playback", flush=True)
            self._fire_speaking("start", duration)
            self._play_subprocess(self._get_play_cmd(audio_file))
            self._fire_speaking("end", duration)
            try: os.unlink(audio_file)
            except: pass
            with self.current_text_lock: self.current_text = ""
            self.busy.clear()
            if self.on_end:
                try: self.on_end()
                except Exception as e: print(f"[TTS] on_end error: {e}")

    def _generate_audio(self, text):
        backend = self._active_backend
        if backend == "pico2wave":
            wav_file = self._generate_wav_pico(text)
            if wav_file:
                dur = get_audio_duration(wav_file) or 0.0
                return wav_file, dur
            return None, 0.0
        mp3_file = self._generate_mp3(text)
        if not mp3_file:
            return None, 0.0
        dur = get_audio_duration(mp3_file) or 0.0
        if backend == "aplay":
            wav_file = self._mp3_to_wav(mp3_file)
            if wav_file:
                try: os.unlink(mp3_file)
                except: pass
                dur = get_audio_duration(wav_file) or dur
                return wav_file, dur
            return None, 0.0
        return mp3_file, dur

    def _get_play_cmd(self, audio_file):
        backend = self._active_backend
        if backend == "paplay": return ["paplay", audio_file]
        elif backend == "mpg123": return ["mpg123", "-q", audio_file]
        elif backend == "ffplay": return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", audio_file]
        elif backend == "aplay": return ["aplay", "-q", audio_file]
        elif backend == "play": return ["play", "-q", audio_file]
        elif backend == "cvlc": return ["cvlc", "--intf", "dummy", "--play-and-exit", audio_file]
        elif backend == "pico2wave": return ["aplay", "-q", audio_file]
        return ["paplay", audio_file]

    def _speak_sync(self, text):
        audio_file, duration = self._generate_audio(text)
        if not audio_file: return
        self._fire_speaking("start", duration)
        self._play_subprocess(self._get_play_cmd(audio_file))
        self._fire_speaking("end", duration)
        try: os.unlink(audio_file)
        except: pass

    def _play_subprocess(self, cmd):
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            while proc.poll() is None:
                if self.stop_flag.is_set():
                    try: proc.terminate()
                    except: pass
                    try: proc.wait(timeout=0.5)
                    except: pass
                    break
                time.sleep(0.05)
        except Exception as e:
            print(f"[TTS] play error: {e}")

    def _generate_mp3(self, text):
        if not EDGE_TTS_AVAILABLE: return None
        try:
            async def _generate():
                communicate = edge_tts.Communicate(text[:3000], self.voice)
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
                    return tf.name, communicate
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                mp3_file, communicate = loop.run_until_complete(_generate())
                loop.run_until_complete(communicate.save(mp3_file))
                return mp3_file
            finally:
                try: loop.close()
                except: pass
        except Exception as e:
            print(f"[TTS] edge-tts error: {e}")
            return None

    def _generate_wav_pico(self, text):
        if "pico2wave" not in self._tools: return None
        try:
            wav_file = tempfile.mktemp(suffix=".wav")
            result = subprocess.run(["pico2wave", "-w", wav_file, "-l", "en-GB", text[:1000]],
                                    capture_output=True, timeout=10)
            if result.returncode == 0 and os.path.exists(wav_file): return wav_file
        except Exception: return None

    def _mp3_to_wav(self, mp3_file):
        if "ffmpeg" not in _audio_tools: return None
        try:
            wav_file = tempfile.mktemp(suffix=".wav")
            result = subprocess.run(["ffmpeg", "-y", "-i", mp3_file, "-ar", "16000", "-ac", "1", wav_file],
                                    capture_output=True, timeout=30)
            if result.returncode == 0 and os.path.exists(wav_file): return wav_file
        except Exception: return None

class VoiceManager:
    WAKE_VARIANTS = {
        "adam": ["atom", "atam", "addam", "adams", "aiden", "admin", "atm"],
        "jarvis": ["travis", "service", "jarvus", "jarves"],
        "computer": ["compter", "computor", "compute"],
    }

    WAKE_LOOP = "WAKE_LOOP"
    COMMAND_RECORD = "COMMAND_RECORD"
    ACTIVE_LISTEN = "ACTIVE_LISTEN"
    MUTED = "MUTED"

    def __init__(self, config, callbacks):
        self.config = config
        self.callbacks = callbacks
        self.running = False
        self.mode = self.WAKE_LOOP
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self.whisper_model = None
        self.initialized = False
        self._sample_rate = int(config.get("sample_rate", 16000))
        self._channels = int(config.get("channels", 1))
        self._chunk = int(config.get("audio_chunk_size", 1024))
        self._mic_device = config.get("mic_device", "pulse")
        self._arecord_proc = None
        self._current_frames = []
        self._frames_lock = threading.Lock()
        self._reader_thread = None
        self._reader_stop = threading.Event()
        self._tts_active = threading.Event()
        self._post_tts_until = 0.0
        self._echo_buffer_seconds = float(config.get("echo_buffer_seconds", 2.5))
        self._echo_suppression = bool(config.get("echo_suppression", True))
        self._wake_check_interval = float(config.get("wake_check_interval", 1.0))
        self._phrase_time_limit_cmd = int(config.get("phrase_time_limit_cmd", 12))
        self._pause_threshold = float(config.get("pause_threshold", 1.2))
        self._energy_threshold = int(config.get("energy_threshold", 300))
        self._active_idle_start = None
        self._active_idle_timeout = 30.0

    def set_mode(self, mode):
        with self._lock: self.mode = mode

    def get_mode(self):
        with self._lock: return self.mode

    def _call(self, name, *args):
        cb = self.callbacks.get(name)
        if cb:
            try: cb(*args)
            except: pass

    def on_tts_event(self, event, duration=0.0):
        if event == "start":
            self._tts_active.set()
            with self._frames_lock: self._current_frames = []
            self.set_mode(self.MUTED)
        elif event == "end":
            self._tts_active.clear()
            self._post_tts_until = time.time() + self._echo_buffer_seconds
            with self._frames_lock: self._current_frames = []
            self.set_mode(self.WAKE_LOOP)

    def initialize_hardware(self):
        # Two independent subsystems: microphone capture (arecord) and
        # offline speech recognition (Whisper). They used to be coupled,
        # which meant a Whisper load failure (e.g. PyTorch/CUDA mismatch,
        # missing model weights, or no GPU) took the entire voice path
        # down. Decoupled: the mic initializes if arecord works; Whisper
        # is best-effort and its failure is surfaced as a status message,
        # not a fatal init error.
        if "arecord" not in _audio_tools:
            return False, "arecord not found (install alsa-utils)"

        # --- 1. Test the microphone hardware ---
        try:
            test_proc = subprocess.Popen(
                ["arecord", "-D", self._mic_device, "-f", "S16_LE", "-r", str(self._sample_rate),
                 "-c", str(self._channels), "-d", "1", "-q", "/tmp/sepiantest.wav"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            test_proc.wait(timeout=3)
            if test_proc.returncode != 0:
                err = test_proc.stderr.read().decode("utf-8", errors="replace").strip()
                return False, f"Microphone test failed (device={self._mic_device}): {err}"
        except Exception as e:
            return False, f"Microphone test failed: {e}"

        # --- 2. Best-effort Whisper load (do NOT abort mic init on failure) ---
        whisper_msg = "loaded"
        if WHISPER_AVAILABLE and whisper is not None:
            model_name = self.config.get("whisper_model", "tiny")
            self._call("on_status", f"Loading whisper '{model_name}'...")
            try:
                whisper_device = self.config.get("whisper_device", "cpu")
                self.whisper_model = whisper.load_model(model_name, device=whisper_device)
                whisper_msg = f"loaded '{model_name}' on {whisper_device}"
            except Exception as e:
                # Log loudly so the user knows why wake-word / STT is disabled.
                print(f"[Voice] Whisper load failed; mic will work but wake-word "
                      f"detection is disabled: {e}", flush=True)
                self.whisper_model = None
                whisper_msg = f"disabled ({type(e).__name__}: {str(e)[:80]})"
        else:
            whisper_msg = "not installed (optional)"
            self.whisper_model = None

        self.initialized = True
        msg = f"Microphone ready (device={self._mic_device}; whisper {whisper_msg})"
        return True, msg

    def start(self):
        if self.running: return
        if not self.initialized:
            ok, msg = self.initialize_hardware()
            if not ok:
                self._call("on_error", msg)
                return
        self.running = True
        self._stop_event.clear()
        self._reader_stop.clear()
        self._start_recorder()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.running = False
        self._stop_event.set()
        self._reader_stop.set()
        self._stop_recorder()

    def _start_recorder(self):
        try:
            cmd = ["arecord", "-D", self._mic_device, "-f", "S16_LE", "-r", str(self._sample_rate), "-c", str(self._channels), "-q"]
            self._arecord_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            self._reader_thread = threading.Thread(target=self._read_audio, daemon=True)
            self._reader_thread.start()
        except Exception as e:
            print(f"[Voice] arecord start error: {e}")

    def _stop_recorder(self):
        if self._arecord_proc:
            try:
                self._arecord_proc.terminate()
                self._arecord_proc = None
            except: pass

    def _read_audio(self):
        chunk_bytes = self._chunk * self._channels * 2
        try:
            while not self._reader_stop.is_set() and self._arecord_proc and self._arecord_proc.poll() is None:
                data = self._arecord_proc.stdout.read(chunk_bytes)
                if not data: break
                if self._echo_suppression and self._is_muted(): continue
                with self._frames_lock:
                    self._current_frames.append(data)
                    max_frames = int(self._sample_rate / self._chunk * 30)
                    if len(self._current_frames) > max_frames:
                        self._current_frames = self._current_frames[-max_frames:]
        except Exception as e:
            print(f"[Voice] read error: {e}")

    def _is_muted(self):
        return self._tts_active.is_set() or time.time() < self._post_tts_until

    def _drain_frames(self):
        with self._frames_lock:
            frames = self._current_frames
            self._current_frames = []
        return list(frames)

    def _fuzzy_match(self, a, b):
        if not a or not b: return False
        if abs(len(a) - len(b)) > 2: return False
        threshold = 0.65 + (0.25 * self.config.get("wake_sensitivity", 0.5))
        return SequenceMatcher(None, a, b).ratio() >= threshold

    def _contains_wake(self, text):
        if not text: return False
        wake = self.config.get("wake_word", "adam").lower().strip()
        text_lower = text.lower()
        if wake in text_lower: return True
        words = re.findall(r"[a-z']+", text_lower)
        variants = self.WAKE_VARIANTS.get(wake, [])
        for w in words:
            if w == wake or w in variants or self._fuzzy_match(w, wake): return True
        return False

    def _frames_to_wav(self, frames):
        if not frames: return None
        wav_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                wav_path = tf.name
            import wave
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(self._channels)
                wf.setsampwidth(2)
                wf.setframerate(self._sample_rate)
                wf.writeframes(b"".join(frames))
        except Exception: return None
        return wav_path

    def _transcribe_frames(self, frames):
        if not self.whisper_model or not frames: return None
        wav_path = self._frames_to_wav(frames)
        if not wav_path: return None
        try:
            result = self.whisper_model.transcribe(wav_path, fp16=False, language="en")
            return result.get("text", "").strip()
        except Exception: return None
        finally:
            try: os.unlink(wav_path)
            except: pass

    def _calc_rms(self, data_bytes):
        if not data_bytes: return 0
        count = len(data_bytes) // 2
        if count == 0: return 0
        try:
            samples = struct.unpack(f"<{count}h", data_bytes)
            sum_squares = sum(s * s for s in samples)
            return int(math.sqrt(sum_squares / count))
        except Exception: return 0

    def _record_one_command(self):
        chunks_per_second = self._sample_rate / self._chunk
        rms_threshold = max(300, self._energy_threshold // 2)
        silence_chunks_needed = int(self._pause_threshold * chunks_per_second)

        self._call("on_status", "Listening...")
        self._drain_frames()

        speech_started = False
        for _ in range(int(5 * chunks_per_second)):
            if self._stop_event.is_set(): return None
            with self._frames_lock:
                if self._current_frames:
                    speech_started = True
                    break
            time.sleep(0.02)
        if not speech_started:
            return None

        frames_buffer = list(self._drain_frames())
        silent_chunks = 0
        start_time = time.time()

        while not self._stop_event.is_set():
            if self._tts_active.is_set(): return None
            with self._frames_lock:
                chunk_data = self._current_frames.pop(0) if self._current_frames else None

            if chunk_data is None:
                time.sleep(0.02)
                if time.time() - start_time > self._phrase_time_limit_cmd: break
                continue

            frames_buffer.append(chunk_data)
            rms = self._calc_rms(chunk_data)
            if rms > rms_threshold:
                silent_chunks = 0
            else:
                silent_chunks += 1
                if silent_chunks >= silence_chunks_needed: break
            if time.time() - start_time > self._phrase_time_limit_cmd: break

        return self._transcribe_frames(frames_buffer)

    def _run(self):
        sample_rate = self._sample_rate
        chunk = self._chunk
        chunks_per_second = sample_rate / chunk

        rms_threshold = max(300, self._energy_threshold // 2)

        wake = self.config.get("wake_word", "adam")
        self._call("on_status", f"Say '{wake}' to wake me up...")

        while not self._stop_event.is_set():
            mode = self.get_mode()
            try:
                if self._tts_active.is_set() or time.time() < self._post_tts_until:
                    time.sleep(0.1)
                    self._drain_frames()
                    continue

                if mode == self.WAKE_LOOP:
                    target_chunks = max(1, int(self._wake_check_interval * chunks_per_second))
                    self._wait_for_frames(target_chunks, timeout=self._wake_check_interval + 1)
                    frames = self._drain_frames()
                    if not frames: continue

                    rms_values = [self._calc_rms(f) for f in frames]
                    if max(rms_values) < 100: continue

                    text = self._transcribe_frames(frames)
                    if text and self._contains_wake(text):
                        self._call("on_wake_word", text)
                        self.set_mode(self.COMMAND_RECORD)

                elif mode == self.COMMAND_RECORD:
                    cmd_text = self._record_one_command()
                    if cmd_text:
                        self._call("on_command", cmd_text.strip())
                    self.set_mode(self.WAKE_LOOP)
                    self._call("on_status", f"Say '{wake}'...")

                elif mode == self.ACTIVE_LISTEN:
                    if self._active_idle_start is None:
                        self._active_idle_start = time.time()

                    cmd_text = self._record_one_command()

                    if self._stop_event.is_set():
                        break

                    if cmd_text:
                        self._call("on_command", cmd_text.strip())
                        self._active_idle_start = time.time()
                    else:
                        if self._active_idle_start and \
                           (time.time() - self._active_idle_start) > self._active_idle_timeout:
                            print("[Voice] ACTIVE_LISTEN idle timeout -> WAKE_LOOP", flush=True)
                            self.set_mode(self.WAKE_LOOP)
                            self._active_idle_start = None
                            self._call("on_status", f"Say '{wake}'...")

                elif mode == self.MUTED:
                    time.sleep(0.1)
                    if not self._tts_active.is_set() and time.time() >= self._post_tts_until:
                        self.set_mode(self.WAKE_LOOP)

            except Exception as e:
                print(f"[Voice] loop error: {e}")
                time.sleep(1)

    def _wait_for_frames(self, n, timeout=5):
        start = time.time()
        while time.time() - start < timeout:
            with self._frames_lock:
                if len(self._current_frames) >= n: return True
            if self._stop_event.is_set(): return False
            time.sleep(0.05)
        return False

class VisualHead:
    def __init__(self, canvas):
        self.canvas = canvas
        self.state = "IDLE"
        self.gaze = "center"
        self._blink_active = False
        self._next_blink_at = time.time() + random.uniform(2.5, 5.0)
        self._blink_until = 0.0
        self.images = {}
        self.gaze_images = {}
        self.viseme_files = ["O", "U", "E", "F", "K", "L", "M", "S", "SH", "TH"]
        self.layer_gaze = None
        self.layer_blink = None
        self.layer_overlay = None
        self._state_lock = threading.Lock()
        self._viseme_seq = []
        self._viseme_idx = 0
        self._viseme_start = 0.0
        self._tts_text = ""
        self._tts_text_lock = threading.Lock()
        self._lip_sync_active = False
        self._lip_sync_duration = 0.0

        if not PIL_AVAILABLE: return
        try:
            try:
                base_img = Image.open(os.path.join(BASE_DIR, "default.png")).convert("RGBA")
                self.images['base'] = ImageTk.PhotoImage(base_img)
                self.canvas.config(width=base_img.size[0], height=base_img.size[1])
                self.canvas.create_image(0, 0, image=self.images['base'], anchor="nw")
            except Exception as e:
                print(f"[HEAD] Failed to load base image: {e}", flush=True)
                return
            for name in ['ai', 'blink'] + self.viseme_files:
                fname = {'ai': 'AI.png', 'blink': 'Eyes-closed.png'}.get(name, f"{name}.png")
                try:
                    self.images[name] = ImageTk.PhotoImage(Image.open(os.path.join(BASE_DIR, fname)).convert("RGBA"))
                except Exception as e:
                    print(f"[HEAD] Could not load {fname}: {e}", flush=True)
            gaze_files = {
                "left": ("lookingleft.png",),
                "middleleft": ("middleleft.png", "midleft.png", "middleleft.jpeg", "midleft.jpeg"),
                "center": ("middlelook.png",),
                "middleright": ("middleright.png", "midright.png", "middleright.jpeg", "midright.jpeg"),
                "right": ("lookingright.png",),
            }
            for gaze, candidates in gaze_files.items():
                gaze_path = next(
                    (os.path.join(BASE_DIR, name)
                     for name in candidates
                     if os.path.exists(os.path.join(BASE_DIR, name))),
                    None,
                )
                if gaze_path:
                    self.gaze_images[gaze] = ImageTk.PhotoImage(
                        Image.open(gaze_path).convert("RGBA")
                    )
            if "left" in self.gaze_images and "right" not in self.gaze_images:
                left_img = Image.open(
                    os.path.join(BASE_DIR, "lookingleft.png")
                ).convert("RGBA")
                self.gaze_images["right"] = ImageTk.PhotoImage(
                    left_img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                )
            self.layer_gaze = self.canvas.create_image(0, 0, image=None, anchor="nw")
            self.layer_blink = self.canvas.create_image(0, 0, image=None, anchor="nw")
            self.layer_overlay = self.canvas.create_image(0, 0, image=None, anchor="nw")
        except Exception as e:
            print(f"Head init: {e}")

    def set_state(self, state):
        with self._state_lock:
            self.state = state
            if state != "SPEAKING":
                self._viseme_seq = []
                self._viseme_idx = 0
                self._lip_sync_active = False

    def set_tts_text(self, text):
        with self._tts_text_lock:
            self._tts_text = text

    def set_gaze(self, gaze):
        """Set the eye layer to one of the five horizontal gaze positions."""
        if gaze not in ("center", "middleleft", "middleright", "left", "right"):
            gaze = "center"
        with self._state_lock:
            self.gaze = gaze

    def start_lip_sync(self, duration_seconds=0.0):
        with self._tts_text_lock:
            text = self._tts_text
        target_ms = duration_seconds * 1000.0 if duration_seconds > 0 else None
        with self._state_lock:
            self._viseme_seq = text_to_viseme_sequence(text, target_ms)
            self._viseme_idx = 0
            self._viseme_start = time.time()
            self._lip_sync_active = True
            self._lip_sync_duration = duration_seconds
        print(f"[HEAD] Lip sync: {len(self._viseme_seq)} visemes, dur={duration_seconds:.2f}s", flush=True)

    def stop_lip_sync(self):
        with self._state_lock:
            self._lip_sync_active = False
            self._viseme_seq = []
            self._viseme_idx = 0
        print(f"[HEAD] Lip sync stopped", flush=True)

    def clear_tts_text(self):
        with self._tts_text_lock: self._tts_text = ""
        with self._state_lock:
            self._viseme_seq = []
            self._viseme_idx = 0
            self._lip_sync_active = False

    def _get_current_viseme(self):
        with self._state_lock:
            seq = self._viseme_seq
            idx = self._viseme_idx
            start = self._viseme_start
            active = self._lip_sync_active
            duration = self._lip_sync_duration

        if not seq or not active:
            return None

        now = time.time()
        elapsed_ms = (now - start) * 1000.0

        if duration > 0 and elapsed_ms >= duration * 1000.0:
            return seq[-1][0] if seq else None

        total_seq_ms = sum(d for _, d in seq)
        if total_seq_ms <= 0:
            return seq[0][0] if seq else None

        position_ms = elapsed_ms % total_seq_ms

        cumulative = 0
        for i, (viseme, dur) in enumerate(seq):
            cumulative += dur
            if position_ms < cumulative:
                if i != idx:
                    with self._state_lock:
                        self._viseme_idx = i
                return viseme

        return seq[-1][0] if seq else None

    def update_animation(self):
        if not self.images or self.layer_overlay is None: return
        with self._state_lock:
            state = self.state
            gaze = self.gaze
        now = time.time()
        if state == "SPEAKING":
            self._blink_active = False
            self._blink_until = 0.0
            self._next_blink_at = now + random.uniform(2.5, 5.0)
        elif now >= self._next_blink_at:
            self._blink_active = True
            self._blink_until = now + 0.16
            self._next_blink_at = now + random.uniform(2.5, 5.0)
        if self._blink_active and now >= self._blink_until:
            self._blink_active = False
        current = None
        if state == "THINKING": current = self.images.get('ai')
        elif state == "SPEAKING":
            viseme_name = self._get_current_viseme()
            if viseme_name and viseme_name in self.images: current = self.images.get(viseme_name)
        try:
            if self.layer_gaze is not None:
                self.canvas.itemconfig(
                    self.layer_gaze, image=self.gaze_images.get(gaze)
                )
            if self.layer_blink is not None:
                self.canvas.itemconfig(
                    self.layer_blink,
                    image=self.images.get("blink") if self._blink_active else "",
                )
            self.canvas.itemconfig(self.layer_overlay, image=current)
        except: pass

try:
    from plugin_manager import PluginManager as _RealPluginManager
    PLUGIN_MANAGER_AVAILABLE = True
except ImportError:
    PLUGIN_MANAGER_AVAILABLE = False
    class _RealPluginManager:
        def __init__(self, *a, **k): self.commands = []
        def register(self, p): pass
        def load_from_directory(self): pass
        def get_all_commands(self): return []
        def get_all_plugins(self): return []
        def try_voice_command(self, text): return None
        def execute_command(self, *a, **k): return {"error": "plugins not installed"}

PluginManager = _RealPluginManager

class SepianApp:
    def __init__(self, master):
        self.master = master
        self._shutting_down = False
        self.cancel_event = threading.Event()
        self.messages = []
        # Per-user-turn counter that caps the lie-refusal retry loop so a
        # stubborn model can't infinite-loop. Reset on each new _send().
        self._claim_retry_used = 0
        # Same idea for stuck-empty-stream: count consecutive empty
        # replies across the recursive _query_llm calls in one turn.
        self._empty_replies = 0
        self._used_thinking_fallback = False
        self.cfg = self._load_config()
        self.server_url = self.cfg["server_url"]
        self.model_name = self.cfg["model"]
        self.server_type = self.cfg["server_type"]
        self.wake_word = self.cfg["wake_word"]
        self.tts_voice = self.cfg["tts_voice"]
        self.tts_backend = self.cfg.get("tts_backend", "auto")
        self.tts_debug = self.cfg.get("tts_debug", True)
        self.echo_suppression = self.cfg.get("echo_suppression", True)
        self.system_prompt = self.cfg["system_prompt"]
        self._chunk_buffer = ""
        self._chunk_flush_id = None
        self._thinking_buffer = ""
        self._thinking_flush_id = None
        self._ui_queue = queue.Queue()
        self._pending_image = None          # PIL.Image or None
        self._pending_image_thumb = None    # ImageTk.PhotoImage for preview
        self._chat_image_refs = []          # keep PhotoImage refs alive in chat
        # Modal dedupe: when an approval request is in flight, the modal
        # Toplevel is tracked here so the Pending button / a second model
        # request can raise the existing dialog instead of opening a
        # duplicate that gets hidden behind the first one (the
        # "stuck until timeout" symptom).
        self._approval_modal_lock = threading.Lock()
        self._approval_modal = None         # tk.Toplevel or None
        self._approval_modal_owner = ""     # "shell" | "dev" | ""
        self._pump_ui()

        self.plugin_manager = PluginManager(plugin_dir="plugins")
        self.plugin_manager.config_file = os.path.join(self.plugin_manager.plugin_dir, "plugin_config.json")
        self.plugin_manager.load_from_directory()
        self.plugin_manager.load_config()
        if PLUGIN_MANAGER_AVAILABLE:
            # Avoid duplicate registration: only register classes whose plugin name
            # isn't already known to the manager (load_from_directory already
            # picked up files in ./plugins).
            existing_names = {getattr(p, "name", type(p).__name__)
                              for p in self.plugin_manager.get_all_plugins()}
            for module_path, class_name in [
                ("plugins.firetv_plugin", "FireTVPlugin"),
                ("plugins.manager_plugin", "ManagerPlugin"),
                ("plugins.self_dev_plugin", "SelfDevPlugin"),
                ("plugins.approved_shell_plugin", "ApprovedShellPlugin"),
            ]:
                try:
                    mod = __import__(module_path, fromlist=[class_name])
                    cls = getattr(mod, class_name)
                    inst = cls()
                    inst_name = getattr(inst, "name", class_name)
                    if inst_name in existing_names:
                        continue
                    self.plugin_manager.register(inst)
                    existing_names.add(inst_name)
                except Exception as e:
                    print(f"Plugin {class_name} not loaded: {e}")
                    import traceback
                    traceback.print_exc()

        # SelfDevPlugin: enable it only if dev_mode is on, and wire its
        # approval callback to the main-thread modal.
        self._dev_plugin = self.plugin_manager.get_plugin("SelfDevPlugin")
        if self._dev_plugin is not None:
            self._dev_plugin.set_approval_callback(self._dev_approval_request)
            dev_on = bool(self.cfg.get("dev_mode_enabled", False))
            self._dev_plugin.enabled = dev_on
            if hasattr(self._dev_plugin, "set_config"):
                paths = self.cfg.get("dev_allowed_paths") or ["/home/davel/Public/Sepian-Unified-Workspace"]
                self._dev_plugin.set_config({"allowed_paths": paths})
            print(f"[Sepian] SelfDevPlugin: {'ENABLED' if dev_on else 'disabled'}")
        else:
            print("[Sepian] SelfDevPlugin not loaded")

        cmds = self.plugin_manager.get_all_commands()
        if cmds:
            self.system_prompt += "\n\nAvailable commands:\n" + "\n".join(f"- {c}" for c in cmds[:200])

        # Dev-mode docs (only when enabled) — teaches Sepian the
        # SelfDevPlugin workflow: propose_edit -> human approves -> apply.
        if self.cfg.get("dev_mode_enabled", False):
            # Build a clear "where you can write" reminder so the model
            # doesn't try to write to bogus paths like /plugins/... that
            # will be rejected by the allowed_paths guard.
            allowed_paths = self.cfg.get("dev_allowed_paths") or \
                ["/home/davel/Public/Sepian-Unified-Workspace"]
            paths_list = "\n".join(f"  - {p}" for p in allowed_paths)
            self.system_prompt += (
                "\n\nSEPIAN SELF-DEVELOPMENT (dev_mode is ENABLED):\n"
                "You have a SelfDevPlugin that can read/search files in the\n"
                "workspace, PROPOSE code edits, and run tests. Every write or\n"
                "test requires the user to click Approve in a modal dialog\n"
                "before it runs. NEVER bypass the approval flow.\n"
                f"\nALLOWED_PATHS (you may ONLY read/write files under these):\n{paths_list}\n"
                "When using list_files/read_file/write_file/propose_edit, "
                "the 'path' argument MUST be either an absolute path under one "
                "of the ALLOWED_PATHS above, or a relative path that resolves "
                "under one of them. NEVER guess or invent a path — if you are "
                "unsure, call SelfDevPlugin.list_files first to see what exists, "
                "then use the path it returns verbatim.\n"
                "Pick the right tool for the task (ROUTING):\n"
                "  - 'Read/inspect/list/find files' -> SelfDevPlugin.list_files,\n"
                "    read_file, or search_code.\n"
                "  - 'Edit an EXISTING file (change contents, fix code)'\n"
                "    -> SelfDevPlugin.propose_edit with a unique old_text anchor.\n"
                "  - 'CREATE a new file (write a story, save notes, create a\n"
                "    new script)' -> SelfDevPlugin.propose_edit with\n"
                "    create_if_missing=True, old_text=\"\", new_text=<full contents>.\n"
                "    This is the correct tool for 'write a fairy tale',\n"
                "    'create a new file called X', etc.\n"
                "  - 'WRITE a file (new or overwrite)' -> SelfDevPlugin.write_file\n"
                "    with path=<path>, content=<full contents>. This is the\n"
                "    SIMPLEST way to write a file and is preferred over\n"
                "    propose_edit for plain write/overwrite tasks.\n"
                "  - 'Append to an existing file' -> SelfDevPlugin.write_file\n"
                "    with mode='append', path=<path>, content=<new text>.\n"
                "    NEVER use shell append (>>) via run_command; it is\n"
                "    blocked by the shell plugin.\n"
                "  - 'Run a shell command, build, install, git, system ops'\n"
                "    -> ApprovedShellPlugin.run_command (with approval).\n"
                "  - 'Run a test' -> SelfDevPlugin.run_test (with approval).\n"
                "  - 'Control smart-home / TV / lights' -> HomeAssistantPlugin,\n"
                "    FireTVPlugin (no approval needed; safe operations).\n"
                "HARD RULE — NEVER use ApprovedShellPlugin.run_command to\n"
                "WRITE or APPEND to a file. Shell redirects (>, >>), tee,\n"
                "and any command whose only purpose is to write a file are\n"
                "REJECTED by the shell plugin with a clear error. If you want\n"
                "to write or append a file, ALWAYS use SelfDevPlugin.propose_edit.\n"
                "The shell plugin is for actual shell commands (ls, git, npm,\n"
                "build, test runners that aren't in SelfDevPlugin.run_test,\n"
                "etc.) — NOT for file I/O.\n"
                "Workflow for any WRITE (propose_edit or run_command):\n"
                "  1. Investigate first with read_file / list_files if needed.\n"
                "  2. Call the matching tool with EXACTLY one well-formed\n"
                "     <tool_call>...</tool_call> block. Include a 'rationale'.\n"
                "  3. The plugin opens a single Approve/Deny modal and BLOCKS\n"
                "     until the user clicks. If approved, the edit is applied\n"
                "     before the tool returns. If denied, the tool returns\n"
                "     an error and the pending entry is dropped — you do NOT\n"
                "     need to call approve_edit or reject_edit afterwards.\n"
                "  4. Only call approve_edit / apply_pending / reject_edit if\n"
                "     the user explicitly asks you to revisit a queued edit\n"
                "     (e.g. they saw a 'Pending (N edit)' badge and told you\n"
                "     to approve it).\n"
                "  5. As a chat fallback, the user can type '/approve <id>'\n"
                "     or '/reject <id>' (only relevant for explicitly-queued\n"
                "     edits, NOT for normal propose_edit calls).\n"
                "If dev_mode is OFF and the user asks you to edit files,\n"
                "tell them to click the 'Dev Mode' button in the toolbar\n"
                "first, then re-issue the request. Never edit sepianai.py or\n"
                "any file outside the configured allowed_paths. If you do\n"
                "edit sepianai.py, the app will offer a /restart — do not\n"
                "auto-restart, wait for the user to click it."
            )
        # Per-plugin example tool calls so the model has a concrete template.
        # We pick representative commands rather than always the first one,
        # because picking list_files as the SelfDevPlugin template trained
        # the model to call list_files when the user asks for a write.
        example_lines = ["\n\nExample tool calls (one per plugin):"]
        # When dev_mode is on, put SelfDevPlugin FIRST so the model sees
        # propose_edit (the correct file-write tool) before run_command.
        # Without this, the model tends to default to run_command and try
        # shell redirects (>, >>) — which the shell plugin rejects.
        all_plugins = list(self.plugin_manager.get_all_plugins())
        if bool(self.cfg.get("dev_mode_enabled", False)):
            all_plugins.sort(
                key=lambda p: (0 if p.name == "SelfDevPlugin" else 1, p.name))
        for plugin in all_plugins:
            try:
                pcmds = plugin.get_commands()
            except Exception:
                pcmds = []
            if not pcmds:
                continue
            # Pick a representative command for each plugin. We want the
            # one the model is MOST LIKELY to need to call, not just the
            # first entry in the command list.
            if plugin.name == "SelfDevPlugin":
                # The headline use-case is editing/creating files. Show
                # propose_edit twice: existing-file and new-file variants.
                # write_file is the simplest file-write path; show it too.
                example_lines.append(
                    f"- {plugin.name}.write_file (preferred for plain file writes): "
                    f'<tool_call>\n{{"tool": "{plugin.name}", "command": "write_file", '
                    f'"args": {{"path": "/path/to/story.txt", '
                    f'"content": "full file contents", '
                    f'"rationale": "why"}}}}'
                    f'\n</tool_call>'
                )
                example_lines.append(
                    f"- {plugin.name}.propose_edit (edit existing file): "
                    f'<tool_call>\n{{"tool": "{plugin.name}", "command": "propose_edit", '
                    f'"args": {{"path": "/path/to/file.py", "old_text": "unique block", '
                    f'"new_text": "replacement block", "rationale": "why"}}}}'
                    f'\n</tool_call>'
                )
                example_lines.append(
                    f"- {plugin.name}.propose_edit (create NEW file): "
                    f'<tool_call>\n{{"tool": "{plugin.name}", "command": "propose_edit", '
                    f'"args": {{"path": "/path/to/newfile.txt", "old_text": "", '
                    f'"new_text": "full file contents", '
                    f'"rationale": "why", "create_if_missing": true}}}}'
                    f'\n</tool_call>'
                )
                example_lines.append(
                    f"- {plugin.name}.read_file (investigate): "
                    f'<tool_call>\n{{"tool": "{plugin.name}", "command": "read_file", '
                    f'"args": {{"path": "/path/to/file.py"}}}}\n</tool_call>'
                )
            elif plugin.name == "ApprovedShellPlugin":
                # Make it explicit that run_command is for SHELL COMMANDS,
                # not for writing files. Use git status / ls / npm test as
                # the example rather than something that looks like a file
                # write.
                example_lines.append(
                    f"- {plugin.name}.run_command (shell op, NOT a file write): "
                    f'<tool_call>\n{{"tool": "{plugin.name}", "command": "run_command", '
                    f'"args": {{"cmd": "git status", "reason": "check repo state"}}}}'
                    f'\n</tool_call>'
                )
                example_lines.append(
                    f"- {plugin.name}.list_approvals: "
                    f'<tool_call>\n{{"tool": "{plugin.name}", "command": "list_approvals", "args": {{}}}}\n</tool_call>'
                )
            else:
                example_lines.append(
                    f"- {plugin.name}.{pcmds[0]}: "
                    f'<tool_call>\n{{"tool": "{plugin.name}", "command": "{pcmds[0]}", "args": {{}}}}\n</tool_call>'
                )
        if len(example_lines) > 1:
            self.system_prompt += "\n".join(example_lines)
        for plugin in self.plugin_manager.get_all_plugins():
            if hasattr(plugin, "set_app"):
                try:
                    plugin.set_app(self)
                except Exception as e:
                    print(f"[Sepian] Failed to bind {plugin.name}: {e}")
            # Wire the approval callback so plugins that need human approval
            # (ApprovedShellPlugin, SelfDevPlugin) can route requests to the
            # Tk main thread instead of touching widgets from worker threads.
            # IMPORTANT: each plugin gets the callback tailored to its own
            # payload schema. Using one callback for both made dev approvals
            # fall through to the shell modal (which only knows about
            # shell_command kind) and silently hang/deny.
            if hasattr(plugin, "set_approval_callback"):
                try:
                    if plugin.name == "SelfDevPlugin":
                        plugin.set_approval_callback(self._dev_approval_request)
                        print("[Sepian] Set approval callback for " + plugin.name + " to dev-style")
                    elif plugin.name == "ApprovedShellPlugin":
                        plugin.set_approval_callback(self._shell_approval_request)
                        print("[Sepian] Set approval callback for " + plugin.name + " to shell-style")
                    else:
                        # Other plugins: default to the shell-style handler
                        # so anything that calls _approval_callback still
                        # gets a working Tk modal.
                        plugin.set_approval_callback(self._shell_approval_request)
                except Exception as e:
                    print(f"[Sepian] Failed to wire approval cb for "
                          f"{plugin.name}: {e}")
        self._setup_theme()
        self._build_ui()
        self._load_chat_history()
        self.tts = TTSManager(voice=self.tts_voice, backend=self.tts_backend, debug=self.tts_debug, echo_suppression=self.echo_suppression)
        self.tts.add_speaking_listener(self._on_tts_audio_event)
        self.voice = None
        try:
            self.voice = VoiceManager(self.cfg, {
                "on_wake_word": lambda text: self._after(0, self._on_wake, text),
                "on_command":   lambda text: self._after(0, self._on_command, text),
                "on_status":    lambda msg:  self._after(0, self._set_status, msg),
                "on_error":     lambda msg:  self._after(0, self._say, f"Voice Error: {msg}"),
            })
            self.tts.add_speaking_listener(self.voice.on_tts_event)
            ok, msg = self.voice.initialize_hardware()
            if ok:
                self.voice.start()
                if self.voice.whisper_model is None:
                    # Mic is recording but wake-word detection is offline.
                    # Be honest about it instead of inviting the user to
                    # say a wake word that nothing will hear.
                    self._set_status(
                        f"Mic ready (device={self.voice._mic_device}) — "
                        "wake-word disabled (no STT backend available)")
                    self._say(
                        "Microphone is on, but offline speech recognition "
                        "(Whisper) couldn't load. Check the terminal for the "
                        "exact error. You can still type in the chat box.",
                        system=True,
                    )
                else:
                    self._set_status(f"Say '{self.wake_word}' to wake me up...")
                print(f"[Sepian] Voice auto-started: {msg}")
            else:
                print(f"[Sepian] Voice auto-start failed: {msg}")
                self.voice = None
        except Exception as e:
            print(f"[Sepian] Voice auto-start error: {e}")
            self.voice = None
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)
        self._animate_head()
        # Run connectivity check first; if the server is unreachable on a
        # fresh install, pop the setup wizard so the user can fix the URL
        # before we burn time trying to prewarm / list models.
        self._after(300, self._check_connectivity_and_setup)
        self._after(1200, self._validate_model)
        self._after(1500, self._prewarm_model)
        # _check_connectivity_and_setup will emit the "Sepian ready" message
        # once it has a confirmed working server, so we don't print it here.

    def _on_tts_audio_event(self, event, duration=0.0):
        if event == "start":
            try:
                self.head.start_lip_sync(duration)
            except: pass
        elif event == "end":
            try:
                self.head.stop_lip_sync()
            except: pass

    def _prewarm_model(self):
        """Send a tiny request to load the model into memory."""
        if not self.cfg.get("preload_on_start", True):
            return
        prompt = self.cfg.get("preload_prompt", "hi")
        self._set_status(f"Warming up {self.model_name}...")

        def do_prewarm():
            try:
                opts = {
                    "num_ctx": self.cfg.get("num_ctx", 4096),
                    "temperature": 0.0,
                    "keep_alive": self.cfg.get("keep_alive", "30m"),
                    "stream_fallback": True,
                    "suppress_thinking": True,
                    "stream_chunk_timeout": 180,
                }
                content, _ = non_streaming_request(
                    self.server_url, self.model_name, self.server_type,
                    [{"role": "user", "content": prompt}],
                    "",
                    600,
                    opts,
                )
                print(f"[Sepian] Prewarm OK ({len(content)} chars).", flush=True)
                self._after(0, self._set_status, "Ready")
            except Exception as e:
                print(f"[Sepian] Prewarm failed (non-fatal): {e}", flush=True)
                self._after(0, self._set_status, "Ready")

        threading.Thread(target=do_prewarm, daemon=True).start()

    def _validate_model(self):
        def check():
            models = list_models(self.server_url, self.server_type)
            if not models:
                self._after(0, self._say, "Could not query server for model list.")
                return
            if self.model_name not in models:
                close = [m for m in models if self.model_name.split(":")[0] in m]
                msg = f"Model '{self.model_name}' not found on server."
                if close:
                    msg += f" Similar: {', '.join(close[:5])}"
                else:
                    msg += f" Available: {', '.join(models[:8])}"
                self._after(0, self._say, msg)
                print(f"[Sepian] {msg}", flush=True)
            else:
                print(f"[Sepian] Model '{self.model_name}' confirmed available.", flush=True)
        threading.Thread(target=check, daemon=True).start()

    def _load_config(self):
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r", encoding="utf-8") as f: cfg = json.load(f)
                for k, v in DEFAULT_CONFIG.items(): cfg.setdefault(k, v)
                return cfg
        except: pass
        return DEFAULT_CONFIG.copy()

    def _save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(self.cfg, f, indent=2)
        except: pass

    def _load_chat_history(self):
        try:
            if CHAT_HISTORY_FILE.exists():
                with open(CHAT_HISTORY_FILE, "r", encoding="utf-8") as f: self.messages = json.load(f)
                # Defensively clean any stale <tool_call> blocks left over
                # from previous versions of the app, so the next turn
                # doesn't see the model's old call as content.
                cleaned = _strip_tool_calls_from_messages(self.messages)
                if cleaned is not self.messages:
                    self.messages = cleaned
                # Same idea for tool results: an older history file may
                # contain 100KB+ read_file payloads. Summarise them on
                # load so we don't blow the context window next turn.
                trimmed = _truncate_tool_results_in_messages(self.messages)
                if trimmed is not self.messages:
                    self.messages = trimmed
        except: self.messages = []

    def _save_chat_history(self):
        try:
            # Build the persisted copy:
            #   - Strip <tool_call> blocks from assistant messages so the
            #     next session doesn't see stale tool calls as content.
            #   - Summarise large tool results (e.g. 100KB read_file
            #     payloads) so the history file stays small and the
            #     next turn doesn't blow past the context window.
            # The raw content stays in self.messages during the live
            # turn (parse_tool_calls + the model's synthesis pass need
            # it).
            base = list(self.messages)
            if not self.cfg.get("save_images_in_history", True):
                # strip image base64 to keep history file small
                base = []
                for m in self.messages:
                    if "images" in m:
                        mm = {k: v for k, v in m.items() if k != "images"}
                        mm["_had_image"] = True
                        base.append(mm)
                    else:
                        base.append(m)
            to_save = _truncate_tool_results_in_messages(
                _strip_tool_calls_from_messages(base)
            )
            with open(CHAT_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(to_save, f, indent=2)
        except: pass

    def _setup_theme(self):
        self.bg = "#000000"
        self.panel = "#111111"
        self.user_col = "#ff3333"
        self.ai_col = "#ff6666"
        self.sys_col = "#ff9999"
        self.fg = "#f0f0f0"
        self.master.configure(bg=self.bg)
        self.font_chat = font.Font(family="Segoe UI", size=11)
        style = ttk.Style()
        try: style.theme_use("clam")
        except: pass

    def _build_ui(self):
        menubar = tk.Menu(self.master)
        self.master.config(menu=menubar)
        m = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Menu", menu=m)
        m.add_command(label="Test Connection", command=self._test_connection)
        m.add_command(label="Test Model", command=self._test_model)
        m.add_command(label="Test TTS (blocking)", command=self._test_tts_blocking)
        m.add_command(label="Test Mic (3s)", command=self._test_mic)
        m.add_command(label="Settings", command=self._show_settings)
        m.add_command(label="Clear Chat", command=self._clear_chat)
        m.add_separator()
        m.add_command(label="Exit", command=self._on_close)

        main_pane = tk.PanedWindow(self.master, orient="horizontal", sashwidth=4, sashrelief="flat", bg="#222222", borderwidth=0)
        main_pane.pack(fill="both", expand=True)
        left_panel = tk.Frame(main_pane, bg=self.bg, width=420, height=460)
        left_panel.pack_propagate(False)
        main_pane.add(left_panel, minsize=420, sticky="nsew")
        self.head_canvas = tk.Canvas(left_panel, width=400, height=450, bg=self.bg, highlightthickness=0)
        self.head_canvas.pack(pady=(5, 5))
        self.head = VisualHead(self.head_canvas)
        self.status_label = tk.Label(left_panel, text="Status: Ready", fg=self.user_col, bg=self.bg, font=("Segoe UI", 10, "bold"))
        self.status_label.pack(pady=(0, 5), fill="x")
        right_panel = tk.Frame(main_pane, bg=self.bg)
        main_pane.add(right_panel, minsize=500, sticky="nsew")
        top = ttk.Frame(right_panel)
        top.pack(side="top", fill="x", padx=10, pady=(10, 5))
        ttk.Label(top, text="Server:").grid(row=0, column=0, padx=(0, 5), sticky="w")
        self.url_entry = ttk.Entry(top)
        self.url_entry.grid(row=0, column=1, sticky="ew", padx=(0, 5))
        self.url_entry.insert(0, self.server_url)
        ttk.Label(top, text="Wake:").grid(row=0, column=2, padx=(10, 5), sticky="w")
        self.wake_entry = ttk.Entry(top, width=10)
        self.wake_entry.grid(row=0, column=3, padx=(0, 5), sticky="w")
        self.wake_entry.insert(0, self.wake_word)
        ttk.Button(top, text="Save", command=self._save_settings).grid(row=0, column=4, padx=2)
        top.columnconfigure(1, weight=1)
        self.chat = scrolledtext.ScrolledText(right_panel, wrap="word", font=self.font_chat, bg=self.panel, fg=self.fg, padx=12, pady=12, state="disabled")
        self.chat.pack(side="top", fill="both", expand=True, padx=10, pady=5)
        self.chat.tag_config("user", foreground=self.user_col)
        self.chat.tag_config("ai", foreground=self.ai_col)
        self.chat.tag_config("system", foreground=self.sys_col)
        self.chat.tag_config("thinking", foreground="#888888")
        bottom = ttk.Frame(right_panel)
        bottom.pack(side="bottom", fill="x", padx=10, pady=10)

        # Image preview row (only shown when an image is attached)
        self._image_preview_frame = ttk.Frame(bottom)
        self._image_preview_label = ttk.Label(self._image_preview_frame)
        self._image_preview_label.pack(side="left", padx=(0, 5))
        ttk.Button(self._image_preview_frame, text="X Remove",
                   command=self._clear_pending_image).pack(side="left")
        self._image_preview_caption = ttk.Label(self._image_preview_frame, text="")
        self._image_preview_caption.pack(side="left", padx=(8, 0))
        self._image_preview_frame.grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 4))
        self._image_preview_frame.grid_remove()

        self.input_entry = ttk.Entry(bottom)
        self.input_entry.grid(row=1, column=0, sticky="ew", ipady=5, padx=(0, 4))
        self.input_entry.bind("<Return>", lambda e: self._send())
        # Ctrl+V: paste image from clipboard if present, else default text paste
        self.input_entry.bind("<Control-v>", self._on_ctrl_v)
        ttk.Button(bottom, text="Attach", command=self._attach_image_from_file).grid(row=1, column=1, padx=2)
        ttk.Button(bottom, text="Listen", command=self._start_voice).grid(row=1, column=2, padx=2)
        ttk.Button(bottom, text="Send",   command=self._send).grid(row=1, column=3, padx=2)
        ttk.Button(bottom, text="Stop",   command=self._interrupt).grid(row=1, column=4, padx=2)

        # ---- Dev/Approval toolbar row (above the input row) ----
        devbar = ttk.Frame(right_panel)
        devbar.pack(side="top", fill="x", padx=10, pady=(0, 0))
        # Dev mode toggle
        self._dev_mode_btn = tk.Button(
            devbar, text=self._dev_mode_button_label(),
            command=self._toggle_dev_mode,
            bg="#1a1a1a", fg="#cccccc",
            activebackground="#333333", activeforeground="#ffffff",
            relief="flat", padx=10, pady=4,
            font=("Segoe UI", 9, "bold"),
        )
        self._dev_mode_btn.pack(side="left", padx=(0, 6))
        # Pending approvals badge. Click action is decided dynamically:
        # if an approval modal is registered but hidden (the classic
        # "stuck pending" symptom), clicking raises the modal; otherwise
        # it opens the Pending Approvals list.
        def _pending_btn_click():
            try:
                state = self._approval_modal_visible()
            except Exception:
                state = "none"
            if state == "hidden":
                self._raise_pending_modal()
            else:
                self._show_pending_approvals()
        self._pending_btn = tk.Button(
            devbar, text=self._pending_button_label(),
            command=_pending_btn_click,
            bg="#1a1a1a", fg="#cccccc",
            activebackground="#dd4444", activeforeground="#ffffff",
            relief="flat", padx=10, pady=4,
            font=("Segoe UI", 9, "bold"),
        )
        self._pending_btn.pack(side="left", padx=(0, 6))
        # Shell quick-test
        ttk.Button(devbar, text="Shell: list_approvals",
                   command=self._shell_quick_list).pack(side="left", padx=(0, 6))
        # Restart hint label (right side, only shown when restart recommended)
        self._restart_hint_label = tk.Label(
            devbar, text="", fg="#ffcc66", bg=self.bg,
            font=("Segoe UI", 9, "italic"),
        )
        self._restart_hint_label.pack(side="right", padx=(6, 0))
        ttk.Button(devbar, text="/restart",
                   command=self._dev_restart).pack(side="right", padx=(4, 0))
        # Periodic badge refresh
        self._refresh_pending_badge()

        bottom.columnconfigure(0, weight=1)

    def _pump_ui(self):
        if self._shutting_down: return
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception as e:
                    print(f"[UI_PUMP] Error: {e}", flush=True)
        except queue.Empty:
            pass
        self.master.after(50, self._pump_ui)

    def _after(self, ms, fn, *args):
        if self._shutting_down: return
        self._ui_queue.put((fn, args))

    def _animate_head(self):
        if not self._shutting_down:
            try:
                self.head.update_animation()
                self.master.after(40, self._animate_head)
            except: self._shutting_down = True

    def _check_connectivity_and_setup(self):
        """First-run / fresh-network check.

        Tries to reach the configured server. If reachable, emits the normal
        "Sepian ready" message and stops. If not reachable, pops the setup
        wizard so the user can either fix the URL, scan the LAN, or proceed
        anyway with the chat-only / approval-gated features.
        """
        if self._shutting_down: return
        url = self.server_url
        try:
            ok, msg = test_server_connection(url, self.server_type)
        except Exception as e:
            ok, msg = False, str(e)
        if ok:
            self._display(
                "System",
                f"Sepian ready. Server: {self.server_type} @ {self.server_url}",
                system=True,
            )
            self._set_status("Ready")
            return
        # Server unreachable. Show the wizard.
        print(f"[Sepian] Server unreachable at {url}: {msg}", flush=True)
        self._set_status("Server unreachable — opening setup wizard")
        self._show_setup_wizard(reason=msg, attempted_url=url)

    def _show_setup_wizard(self, reason="", attempted_url=""):
        """First-run / fresh-network setup dialog.

        Lets the user:
          * Confirm/edit the server URL
          * Auto-scan the local network for an Ollama server (port 11434)
          * Pick a microphone device
          * Test the connection before committing
          * Skip setup and proceed with offline / chat-only features
        """
        if self._shutting_down: return
        win = tk.Toplevel(self.master)
        win.title("Sepian Setup")
        win.geometry("560x520")
        win.configure(bg=self.bg)
        win.transient(self.master)
        win.grab_set()

        title = ttk.Label(
            win,
            text="Sepian couldn't reach the LLM server.",
            font=("Segoe UI", 12, "bold"),
        )
        title.pack(pady=(12, 4))
        if reason:
            ttk.Label(
                win,
                text=f"Reason: {reason}",
                foreground="#aa3333",
                wraplength=520,
                justify="center",
            ).pack(pady=(0, 8))

        # --- Server URL ---
        url_frame = ttk.LabelFrame(win, text="LLM Server")
        url_frame.pack(fill="x", padx=10, pady=6)
        ttk.Label(url_frame, text="URL:").grid(row=0, column=0, padx=5, pady=4, sticky="w")
        url_var = tk.StringVar(value=attempted_url or self.cfg.get("server_url", ""))
        ttk.Entry(url_frame, textvariable=url_var, width=40).grid(
            row=0, column=1, padx=5, pady=4, sticky="ew"
        )

        type_frame = ttk.Frame(url_frame)
        type_frame.grid(row=1, column=0, columnspan=2, padx=5, pady=4, sticky="w")
        ttk.Label(type_frame, text="Type:").pack(side="left", padx=(0, 5))
        type_var = tk.StringVar(value=self.cfg.get("server_type", "ollama"))
        ttk.Combobox(
            type_frame, textvariable=type_var, width=14, state="readonly",
            values=("ollama", "openai"),
        ).pack(side="left")

        result_var = tk.StringVar(value="")
        ttk.Label(
            url_frame, textvariable=result_var, foreground="#888888"
        ).grid(row=2, column=0, columnspan=2, padx=5, pady=(2, 4), sticky="w")

        def do_test():
            url = url_var.get().strip()
            stype = type_var.get().strip()
            result_var.set("Testing...")
            win.update_idletasks()
            try:
                ok, msg = test_server_connection(url, stype)
            except Exception as e:
                ok, msg = False, str(e)
            if ok:
                result_var.set(f"OK — {msg}")
            else:
                result_var.set(f"Failed — {msg}")

        def do_scan():
            """Scan likely local IPs for an Ollama server on port 11434."""
            result_var.set("Scanning local network...")
            win.update_idletasks()
            found = _scan_for_ollama(timeout_per_host=0.4)
            if found:
                url_var.set(found[0])
                result_var.set(f"Found: {found[0]}  (also: {', '.join(found[1:3])})")
            else:
                result_var.set(
                    "No Ollama server found on the local network. "
                    "Enter the URL manually if your server is elsewhere."
                )

        ttk.Button(url_frame, text="Test", command=do_test, width=10).grid(
            row=0, column=2, padx=5, pady=4
        )
        ttk.Button(url_frame, text="Scan network", command=do_scan, width=14).grid(
            row=1, column=2, padx=5, pady=4
        )
        url_frame.columnconfigure(1, weight=1)

        # --- Mic device ---
        mic_frame = ttk.LabelFrame(win, text="Microphone (optional)")
        mic_frame.pack(fill="x", padx=10, pady=6)
        ttk.Label(mic_frame, text="Device:").grid(row=0, column=0, padx=5, pady=4, sticky="w")
        mic_var = tk.StringVar(value=self.cfg.get("mic_device", "default"))
        mic_combo = ttk.Combobox(mic_frame, textvariable=mic_var, width=40)
        mic_combo.grid(row=0, column=1, padx=5, pady=4, sticky="ew")
        # Populate from PortAudio, falling back to "default".
        try:
            import pyaudio
            pa = pyaudio.PyAudio()
            names = ["default"]
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if int(info.get("maxInputChannels", 0)) > 0:
                    names.append(info.get("name", f"device {i}"))
            pa.terminate()
            mic_combo["values"] = names
        except Exception:
            mic_combo["values"] = ("default",)

        def do_test_mic():
            name = mic_var.get().strip()
            result_var.set(f"Testing mic '{name}'...")
            win.update_idletasks()
            try:
                import pyaudio
                pa = pyaudio.PyAudio()
                if name == "default":
                    idx = None
                else:
                    idx = None
                    for i in range(pa.get_device_count()):
                        if pa.get_device_info_by_index(i).get("name") == name:
                            idx = i
                            break
                kwargs = {"format": pyaudio.paInt16, "channels": 1, "rate": 16000,
                          "input": True, "frames_per_buffer": 1024}
                if idx is not None:
                    kwargs["input_device_index"] = idx
                stream = pa.open(**kwargs)
                data = stream.read(1024, exception_on_overflow=False)
                stream.stop_stream()
                stream.close()
                pa.terminate()
                # Rough RMS so we know it isn't silence.
                import struct
                samples = struct.unpack(f"{len(data)//2}h", data)
                rms = (sum(s * s for s in samples) / max(1, len(samples))) ** 0.5
                result_var.set(f"Mic OK — RMS {rms:.0f}")
            except Exception as e:
                result_var.set(f"Mic test failed — {e}")

        ttk.Button(mic_frame, text="Test mic", command=do_test_mic, width=12).grid(
            row=0, column=2, padx=5, pady=4
        )
        mic_frame.columnconfigure(1, weight=1)

        # --- Actions ---
        actions = ttk.Frame(win)
        actions.pack(fill="x", padx=10, pady=(10, 12))

        def do_save_and_close():
            self.cfg["server_url"] = url_var.get().strip() or self.cfg["server_url"]
            self.cfg["server_type"] = type_var.get().strip() or "ollama"
            self.cfg["mic_device"] = mic_var.get().strip() or "default"
            self.server_url = self.cfg["server_url"]
            self.server_type = self.cfg["server_type"]
            if self.voice is not None:
                self.voice._mic_device = self.cfg["mic_device"]
            self._save_config()
            self._display(
                "System",
                f"Sepian ready. Server: {self.server_type} @ {self.server_url}",
                system=True,
            )
            self._set_status("Ready")
            win.destroy()

        def do_skip():
            self._display(
                "System",
                f"Setup skipped. Server still set to {self.server_url}. "
                "You can change it later via Menu > Settings.",
                system=True,
            )
            self._set_status("Setup skipped — server unreachable")
            win.destroy()

        ttk.Button(actions, text="Save & Continue", command=do_save_and_close).pack(
            side="left", padx=5
        )
        ttk.Button(actions, text="Skip (offline mode)", command=do_skip).pack(
            side="left", padx=5
        )
        ttk.Button(actions, text="Open full Settings", command=lambda: (win.destroy(), self._show_settings())).pack(
            side="right", padx=5
        )

        win.protocol("WM_DELETE_WINDOW", do_skip)
        # Center on main window.
        try:
            self.master.update_idletasks()
            x = self.master.winfo_x() + 60
            y = self.master.winfo_y() + 60
            win.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _display(self, sender, msg, system=False):
        if self._shutting_down: return
        try:
            self.chat.configure(state="normal")
            tag = "system" if system else ("user" if sender == "User" else "ai")
            self.chat.insert("end", f"[{sender}] {msg}\n\n", tag)
            self.chat.configure(state="disabled")
            self.chat.see("end")
        except Exception as e:
            print(f"[DISPLAY] error: {e}", flush=True)

    def _display_chunk(self, text):
        self._chunk_buffer += text
        if len(self._chunk_buffer) >= 50:
            self._flush_chunks()
        elif self._chunk_flush_id is None:
            self._chunk_flush_id = self.master.after(80, self._flush_chunks)

    def _flush_chunks(self):
        if self._shutting_down:
            self._chunk_buffer = ""
            self._chunk_flush_id = None
            # Also flush any pending thinking so it doesn't get stuck in
            # the buffer if the app is shutting down mid-stream.
            self._flush_thinking()
            return
        if not self._chunk_buffer:
            self._chunk_flush_id = None
        else:
            try:
                self.chat.configure(state="normal")
                self.chat.insert("end", self._chunk_buffer, "ai")
                self.chat.configure(state="disabled")
                self.chat.see("end")
            except Exception as e:
                print(f"[FLUSH] error: {e}", flush=True)
            finally:
                self._chunk_buffer = ""
                self._chunk_flush_id = None
        # Always also flush any pending thinking so the two streams
        # land on screen together.
        self._flush_thinking()

    def _display_thinking_chunk(self, text):
        """Buffer thinking chunks and flush on size or time, mirroring
        _display_chunk. Prevents per-token "[Thinking] word" spam when
        the model streams thinking word-by-word."""
        self._thinking_buffer += text
        if len(self._thinking_buffer) >= 50:
            self._flush_thinking()
        elif self._thinking_flush_id is None:
            self._thinking_flush_id = self.master.after(
                80, self._flush_thinking)

    def _flush_thinking(self):
        if self._shutting_down:
            self._thinking_buffer = ""
            self._thinking_flush_id = None
            return
        if not self._thinking_buffer:
            self._thinking_flush_id = None
            return
        try:
            self.chat.configure(state="normal")
            self.chat.insert("end", self._thinking_buffer, "thinking")
            self.chat.configure(state="disabled")
            self.chat.see("end")
        except Exception as e:
            print(f"[FLUSH-THINK] error: {e}", flush=True)
        finally:
            self._thinking_buffer = ""
            self._thinking_flush_id = None

    def _set_status(self, text):
        if self._shutting_down: return
        try: self.status_label.config(text=f"Status: {text}")
        except: pass

    def _set_head_state(self, state):
        try: self.head.set_state(state)
        except: pass

    def _say(self, text, system=True):
        self._display("System", text, system=system)
        self.tts.speak(text)

    def _test_tts_blocking(self):
        self._set_status("Testing TTS...")
        def do_test():
            self.tts.speak("Audio test successful.", blocking=True)
            self._after(0, self._set_status, "Ready")
        threading.Thread(target=do_test, daemon=True).start()

    def _test_mic(self):
        self._set_status("Testing mic...")
        def do_test():
            try:
                cmd = ["arecord", "-D", self.cfg.get("mic_device", "pulse"), "-f", "S16_LE", "-r", str(self.cfg.get("sample_rate", 16000)), "-c", str(self.cfg.get("channels", 1)), "-d", "3", "/tmp/sepiantest.wav"]
                subprocess.run(cmd, capture_output=True, timeout=10)
                if WHISPER_AVAILABLE:
                    # Use the already-resolved whisper module from module scope
                    # (avoids re-importing a shadowed top-level `whisper` package).
                    model = whisper.load_model(self.cfg.get("whisper_model", "tiny"), device=self.cfg.get("whisper_device", "cpu"))
                    result = model.transcribe("/tmp/sepiantest.wav", fp16=False)
                    self._after(0, self._say, f"Heard: '{result['text']}'")
                os.unlink("/tmp/sepiantest.wav")
            except Exception as e: self._after(0, self._say, f"Mic test error: {e}")
            self._after(0, self._set_status, "Ready")
        threading.Thread(target=do_test, daemon=True).start()

    def _test_connection(self):
        self._set_status("Testing...")
        def do_test():
            ok, msg = test_server_connection(self.server_url, self.server_type)
            self._after(0, self._say, f"Connection {'OK' if ok else 'FAILED'}: {msg}")
            if ok:
                models = list_models(self.server_url, self.server_type)
                if models:
                    self._after(0, self._say, f"Available models: {', '.join(models[:10])}")
            self._after(0, self._set_status, "Ready")
        threading.Thread(target=do_test, daemon=True).start()

    def _test_model(self):
        self._set_status(f"Testing {self.model_name}...")
        def do_test():
            try:
                opts = {
                    "num_ctx": self.cfg.get("num_ctx", 4096),
                    "temperature": self.cfg.get("temperature", 0.7),
                    "keep_alive": self.cfg.get("keep_alive", "30m"),
                    "stream_fallback": True,
                    "suppress_thinking": False,
                }
                content, thinking = non_streaming_request(
                    self.server_url, self.model_name, self.server_type,
                    [{"role": "user", "content": "Reply with exactly: MODEL_OK"}],
                    self.system_prompt,
                    int(self.cfg.get("request_timeout", 300)),
                    opts,
                )
                msg = f"Model test reply: '{content[:200]}'"
                if thinking:
                    msg += f"\nThinking: {thinking[:200]}"
                self._after(0, self._say, msg)
            except Exception as e:
                self._after(0, self._say, f"Model test FAILED: {e}")
            self._after(0, self._set_status, "Ready")
        threading.Thread(target=do_test, daemon=True).start()

    def _save_settings(self):
        self.server_url = self.url_entry.get().strip()
        self.wake_word = self.wake_entry.get().strip()
        self.cfg["server_url"] = self.server_url
        self.cfg["wake_word"] = self.wake_word
        self._save_config()
        self._say("Settings saved.")

    def _clear_chat(self):
        try:
            self.chat.configure(state="normal")
            self.chat.delete("1.0", "end")
            self.chat.configure(state="disabled")
            self.messages = []
            self._chat_image_refs = []
            self._save_chat_history()
            self._clear_pending_image()
        except: pass

    def _show_settings(self):
        win = tk.Toplevel(self.master)
        win.title("Settings")
        # Height is set to fit a 1080p laptop screen (~720 px of usable
        # space below the taskbar). The content is wrapped in a Canvas +
        # Scrollbar so any overflow remains reachable by scrolling.
        win.geometry("540x720")
        win.resizable(True, True)
        win.configure(bg=self.bg)

        # --- Scrollable body ------------------------------------------------
        # Outer frame holds the canvas (which holds the scrollable area)
        # plus the always-visible Save button row beneath it.
        body_frame = ttk.Frame(win)
        body_frame.pack(fill="both", expand=True)
        body_frame.columnconfigure(0, weight=1)
        body_frame.columnconfigure(1, weight=0)
        body_frame.rowconfigure(0, weight=1)

        canvas = tk.Canvas(body_frame, highlightthickness=0, bg=self.bg)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body_frame, orient="vertical",
                               command=canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scroll.set)

        # `container` is what every body widget is grid()ed onto. It lives
        # inside the canvas window and matches the canvas width so widgets
        # stretch to the window width.
        container = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=container, anchor="nw")

        def _fit_canvas_width(_event=None):
            # Keep the inner frame as wide as the canvas itself.
            canvas.itemconfigure(win_id, width=canvas.winfo_width())

        def _update_scrollregion(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        container.bind("<Configure>", _update_scrollregion)
        canvas.bind("<Configure>", _fit_canvas_width)

        def _on_mousewheel(event):
            # Cross-platform mouse-wheel scrolling (Windows/ macOS delta
            # values differ).
            try:
                if event.delta:
                    canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                else:
                    canvas.yview_scroll(-1 if event.num == 5 else 1, "units")
            except Exception:
                pass

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", _on_mousewheel)
        canvas.bind_all("<Button-5>", _on_mousewheel)

        # Helper: alias `win` -> `container` so all the existing
        # `ttk.Label(win, ...).grid(...)` and `ttk.Entry(win, ...)` calls
        # below render inside the scrollable area without further edits.
        win = container
        win.columnconfigure(0, weight=0)
        win.columnconfigure(1, weight=1)
        row = 0

        # ---- Dev mode (visually distinct, sits at the top) ----
        dev_frame = ttk.LabelFrame(win, text="Self-Development Mode")
        dev_frame.grid(row=row, column=0, columnspan=2, padx=5, pady=(8, 12),
                       sticky="ew")
        row += 1
        self._var_dev_mode_enabled = tk.BooleanVar(
            value=bool(self.cfg.get("dev_mode_enabled", False)))
        ttk.Checkbutton(dev_frame,
                        text="Enable SelfDevPlugin (Sepian can read files and "
                             "propose edits; EVERY edit/test requires your "
                             "approval)",
                        variable=self._var_dev_mode_enabled).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=6, pady=4)
        ttk.Label(dev_frame, text="Allowed paths (one per line):").grid(
            row=1, column=0, sticky="nw", padx=6, pady=(4, 0))
        self._var_dev_allowed_paths = tk.StringVar(
            value="\n".join(self.cfg.get("dev_allowed_paths") or []))
        # Multi-line Text widget so users can actually edit more than one
        # path. The StringVar acts as the load/save handle: we push its
        # value into the Text on show, and pull the Text contents back
        # into the StringVar in save().
        self._dev_paths_text = tk.Text(
            dev_frame, height=4, width=40, wrap="word",
            font=("Segoe UI", 10), bg="#ffffff", fg="#111111",
            insertbackground="#111111", relief="sunken", bd=1,
        )
        self._dev_paths_text.grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        self._dev_paths_text.insert("1.0", self._var_dev_allowed_paths.get())
        self._var_dev_chat_fallback = tk.BooleanVar(
            value=bool(self.cfg.get("dev_chat_fallback_enabled", True)))
        ttk.Checkbutton(dev_frame,
                        text="Allow /approve and /reject in chat as fallback "
                             "approval (when no GUI modal is available)",
                        variable=self._var_dev_chat_fallback).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=6, pady=4)
        ttk.Separator(win, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=4)
        row += 1

        keys = ["server_url", "model", "server_type", "wake_word", "tts_voice", "tts_backend",
                "whisper_model", "energy_threshold", "pause_threshold", "wake_sensitivity",
                "wake_check_interval", "phrase_time_limit_cmd", "mic_device", "sample_rate",
                "channels", "tts_debug", "echo_suppression", "echo_buffer_seconds",
                "num_ctx", "temperature", "keep_alive", "stream_fallback", "suppress_thinking",
                "request_timeout", "stream_chunk_timeout", "force_non_streaming",
                "preload_on_start", "max_retries",
                "image_max_size", "image_jpeg_quality", "save_images_in_history"]
        for key in keys:
            ttk.Label(win, text=key).grid(row=row, column=0, padx=5, pady=2, sticky="w")
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            ttk.Entry(win, textvariable=v, width=40).grid(row=row, column=1, padx=5, pady=2)
            setattr(self, f"_var_{key}", v)
            row += 1
        def save():
            for key in keys:
                v = getattr(self, f"_var_{key}").get().strip()
                if key in ("energy_threshold", "sample_rate", "channels", "phrase_time_limit_cmd",
                           "num_ctx", "request_timeout", "stream_chunk_timeout", "max_retries",
                           "image_max_size", "image_jpeg_quality"):
                    try: self.cfg[key] = int(v)
                    except: pass
                elif key in ("pause_threshold", "wake_sensitivity", "echo_buffer_seconds",
                             "wake_check_interval", "temperature", "retry_backoff"):
                    try: self.cfg[key] = float(v)
                    except: pass
                elif key in ("tts_debug", "echo_suppression", "stream_fallback", "suppress_thinking",
                             "force_non_streaming", "preload_on_start", "save_images_in_history"):
                    self.cfg[key] = v.lower() in ("true", "1", "yes")
                else:
                    self.cfg[key] = v
            # Dev-mode settings
            self.cfg["dev_mode_enabled"] = bool(self._var_dev_mode_enabled.get())
            # Pull from the multi-line Text widget so edits round-trip.
            try:
                paths_text = self._dev_paths_text.get("1.0", "end")
            except Exception:
                paths_text = self._var_dev_allowed_paths.get()
            paths = [p.strip() for p in paths_text.splitlines() if p.strip()]
            self.cfg["dev_allowed_paths"] = paths or [
                "/home/davel/Public/Sepian-Unified-Workspace"]
            self.cfg["dev_chat_fallback_enabled"] = bool(
                self._var_dev_chat_fallback.get())
            # Apply live
            if self._dev_plugin is not None:
                self._dev_plugin.enabled = bool(self.cfg["dev_mode_enabled"])
                if hasattr(self._dev_plugin, "set_config"):
                    self._dev_plugin.set_config(
                        {"allowed_paths": self.cfg["dev_allowed_paths"]})
            self._save_config()
            self.server_url = self.cfg["server_url"]
            self.model_name = self.cfg["model"]
            self.server_type = self.cfg["server_type"]
            self.wake_word = self.cfg["wake_word"]
            self.tts.voice = self.cfg["tts_voice"]
            if self.voice:
                self.voice._mic_device = self.cfg.get("mic_device", "pulse")
                self.voice._energy_threshold = int(self.cfg.get("energy_threshold", 300))
            state = ("ENABLED" if self.cfg["dev_mode_enabled"] else "disabled")
            self._say(f"Settings saved. SelfDevPlugin {state}.")
            # `win` was reassigned to the inner `container` frame above so
            # every existing grid() call would render inside the
            # scrollable canvas area. The real Toplevel we want to close
            # is its ancestor; use win.master to grab it.
            try:
                win.master.destroy()
            except Exception:
                pass
        # Save button lives on the outer body_frame (NOT the scrollable
        # container) so it is always visible even when content overflows.
        ttk.Button(body_frame, text="Save", command=save).grid(
            row=1, column=0, columnspan=2, pady=10, sticky="ew", padx=5)

    def _start_voice(self):
        if self.voice and self.voice.running: return
        self.tts.stop()
        time.sleep(0.3)
        if not self.voice:
            self.voice = VoiceManager(self.cfg, {
                "on_wake_word": lambda text: self._after(0, self._on_wake, text),
                "on_command":   lambda text: self._after(0, self._on_command, text),
                "on_status":    lambda msg:  self._after(0, self._set_status, msg),
                "on_error":     lambda msg:  self._after(0, self._say, f"Voice Error: {msg}"),
            })
            self.tts.add_speaking_listener(self.voice.on_tts_event)
        if not self.voice.initialized:
            ok, msg = self.voice.initialize_hardware()
            if not ok:
                self._say(f"Voice init failed: {msg}")
                return
        self.voice.start()
        self._set_status(f"Say '{self.wake_word}' to wake me up...")

    def _on_wake(self, text):
        self._display("System", f"Wake word heard: '{text}'", system=True)
        self._set_status("Listening for command...")
        self._set_head_state("LISTENING")

    def _on_command(self, cmd_text):
        if not cmd_text: return
        self._display("User", cmd_text)
        self.messages.append({"role": "user", "content": cmd_text})
        self._save_chat_history()
        self._query_llm()

    def _on_ctrl_v(self, event=None):
        """Ctrl+V: if the clipboard has an image, attach it; otherwise let Entry paste normally."""
        if not PIL_AVAILABLE:
            return None
        try:
            img = ImageGrab.grabclipboard()
        except Exception:
            img = None
        if isinstance(img, Image.Image):
            self._set_pending_image(img)
            return "break"  # suppress default text paste
        return None  # allow default text paste

    def _attach_image_from_file(self):
        if not PIL_AVAILABLE:
            self._say("Pillow not installed - image input unavailable.")
            return
        path = filedialog.askopenfilename(
            title="Select image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            img = Image.open(path)
            img.load()
            self._set_pending_image(img)
        except Exception as e:
            self._say(f"Failed to open image: {e}")

    def _set_pending_image(self, img):
        """Store a PIL Image as the next outbound attachment and show preview."""
        if not PIL_AVAILABLE or img is None:
            return
        self._pending_image = img
        try:
            thumb = img.copy()
            thumb.thumbnail((80, 80))
            self._pending_image_thumb = ImageTk.PhotoImage(thumb)
            self._image_preview_label.configure(image=self._pending_image_thumb)
            w, h = img.size
            self._image_preview_caption.configure(text=f"Image attached ({w}x{h})")
            self._image_preview_frame.grid()
        except Exception as e:
            print(f"[Vision] preview error: {e}", flush=True)

    def _clear_pending_image(self):
        self._pending_image = None
        self._pending_image_thumb = None
        try:
            self._image_preview_label.configure(image="")
            self._image_preview_caption.configure(text="")
            self._image_preview_frame.grid_remove()
        except Exception:
            pass

    def _display_user_with_image(self, text, img):
        """Display a user message with an inline image thumbnail in the chat."""
        if self._shutting_down:
            return
        try:
            self.chat.configure(state="normal")
            tag = "user"
            self.chat.insert("end", f"[User] {text or '(image attached)'}\n", tag)
            if PIL_AVAILABLE and img is not None:
                thumb = img.copy()
                thumb.thumbnail((220, 220))
                photo = ImageTk.PhotoImage(thumb)
                self._chat_image_refs.append(photo)  # keep alive
                self.chat.image_create("end", image=photo)
            self.chat.insert("end", "\n\n")
            self.chat.configure(state="disabled")
            self.chat.see("end")
        except Exception as e:
            print(f"[DISPLAY] user+image error: {e}", flush=True)

    def _send(self):
        text = self.input_entry.get().strip()
        if not text and not self._pending_image:
            return

        # Dev-mode chat commands: /approve <id>, /reject <id>, /restart
        if text.startswith("/"):
            low = text.split(maxsplit=1)[0].lower()
            if low in ("/approve", "/reject"):
                # If a shell-approval is pending, /approve /reject resolve it.
                if low in ("/approve", "/reject") and self._handle_shell_chat_command(low):
                    self.input_entry.delete(0, "end")
                    return
                if self._handle_dev_chat_command(text):
                    self.input_entry.delete(0, "end")
                return
            if low == "/sticky":
                if self._handle_shell_chat_command("/sticky"):
                    self.input_entry.delete(0, "end")
                return
            if low == "/restart":
                self._dev_restart()
                self.input_entry.delete(0, "end")
                return

        user_msg = {"role": "user", "content": text or "(image attached)"}

        # Reset the lie-refusal retry budget for the new turn.
        self._claim_retry_used = 0
        # Reset the empty-reply counter too — a fresh user turn starts
        # with a clean budget for catching a wedged cloud endpoint.
        self._empty_replies = 0

        if self._pending_image:
            if not PIL_AVAILABLE:
                self._say("Cannot attach image: Pillow not installed.")
                return
            max_size = int(self.cfg.get("image_max_size", 1024))
            quality = int(self.cfg.get("image_jpeg_quality", 85))
            b64 = image_to_base64(self._pending_image, max_size=max_size, quality=quality)
            if not b64:
                self._say("Failed to encode image.")
                return
            user_msg["images"] = [b64]
            self._display_user_with_image(text, self._pending_image)
        else:
            self._display("User", text)

        self.input_entry.delete(0, "end")
        self.messages.append(user_msg)
        self._save_chat_history()
        self._clear_pending_image()
        self._query_llm()

    def _query_llm(self):
        self.cancel_event.clear()
        self._set_status("Thinking...")
        self._set_head_state("THINKING")
        print(f"\n[QUERY] ====== NEW QUERY to {self.model_name} ======", flush=True)

        opts = {
            "num_ctx": self.cfg.get("num_ctx", 4096),
            "temperature": self.cfg.get("temperature", 0.7),
            "keep_alive": self.cfg.get("keep_alive", "30m"),
            "stream_fallback": self.cfg.get("stream_fallback", True),
            "suppress_thinking": self.cfg.get("suppress_thinking", True),
            "stream_chunk_timeout": self.cfg.get("stream_chunk_timeout", 60),
            "force_non_streaming": self.cfg.get("force_non_streaming", False),
            "max_retries": self.cfg.get("max_retries", 2),
            "retry_backoff": self.cfg.get("retry_backoff", 2.0),
        }

        def do_query():
            try:
                full_reply = ""
                thinking_block = ""
                chunk_count = 0
                tool_calls = 0
                max_tools = int(self.cfg.get("max_tool_calls_per_turn", 3))

                # Convert messages for the active server (handles images for OpenAI format)
                cap_cfg = {
                    "enable_context_capping": bool(self.cfg.get("enable_context_capping", True)),
                    "max_history_messages": int(self.cfg.get("max_history_messages", 24)),
                    "max_history_chars": int(self.cfg.get("max_history_chars", 50_000)),
                }
                msgs_to_send = convert_messages_for_server(
                    self.messages, self.server_type, cap_cfg)

                # DEBUG: dump exact request summary so we can see what the
                # model actually receives. Logs msg count, total char count,
                # and the role sequence. Remove once the history/tool-output
                # bug is resolved.
                try:
                    roles = [m.get("role", "?") for m in msgs_to_send]
                    chars = sum(len(str(m.get("content", ""))) for m in msgs_to_send)
                    print(
                        f"[QUERY-DEBUG] >>> sending {len(msgs_to_send)} msgs "
                        f"({chars} chars) to {self.model_name} via "
                        f"{self.server_type} | roles: {roles}",
                        flush=True,
                    )
                    if roles and roles[-1] != "user":
                        print(
                            f"[QUERY-DEBUG] !!! WARNING: last msg is "
                            f"{roles[-1]}, not user. Model will see a "
                            f"dangling turn and may respond as if first "
                            f"prompt.",
                            flush=True,
                        )
                except Exception as e:
                    print(f"[QUERY-DEBUG] log failed: {e}", flush=True)

                for text_chunk, chunk_type in stream_llm_response(
                    self.server_url, self.model_name, self.server_type,
                    msgs_to_send, self.system_prompt,
                    int(self.cfg.get("request_timeout", 300)),
                    self.cancel_event,
                    opts,
                ):
                    chunk_count += 1
                    if chunk_type == "cancelled":
                        self._after(0, self._flush_chunks)
                        self._after(0, self._set_status, "Interrupted")
                        return
                    elif chunk_type == "thinking":
                        thinking_block += text_chunk
                        if self.cfg.get("show_thinking_to_user", False):
                            # Buffer and flush so a streaming response of
                            # "We need to call X" doesn't render as
                            # "[Thinking] We [Thinking] need [Thinking] to
                            # [Thinking] call [Thinking] X" on screen.
                            self._after(0, self._display_thinking_chunk, text_chunk)
                    elif chunk_type == "content":
                        full_reply += text_chunk
                        self._after(0, self._display_chunk, text_chunk)
                    elif chunk_type == "error":
                        self._after(0, self._flush_chunks)
                        self._after(0, self._say, text_chunk)
                        return

                if not full_reply.strip():
                    if thinking_block:
                        # The cloud model (minimax-m3, Nemotron) sometimes
                        # emits its entire response inside the thinking
                        # field with content="" for every chunk. When that
                        # happens we lose everything if we discard
                        # thinking. Fall back to using the thinking text
                        # as the reply so tool_call parsing can still find
                        # any blocks the model emitted.
                        #
                        # Truncate to a reasonable size to keep the
                        # history from bloating (full thinking can be tens
                        # of KB), but keep it large enough that tool_call
                        # blocks near the end of the response survive.
                        fallback = thinking_block
                        if len(fallback) > 8000:
                            fallback = fallback[:8000]
                        full_reply = (
                            f"[No content stream; using thinking field "
                            f"({len(thinking_block)}c)]\n\n{fallback}"
                        )
                        # Mark this so downstream history/TTS paths know
                        # the reply was synthesized from reasoning, not
                        # from an actual content stream. We must NOT save
                        # the raw reasoning into chat history (it would
                        # poison the next turn: the model reads its own
                        # reasoning as conversation and hallucinates
                        # tool calls that never happened), and we must
                        # NOT TTS-read 4KB of model musing aloud.
                        self._used_thinking_fallback = True
                        print(
                            f"[QUERY] No content from stream; fell back "
                            f"to thinking field ({len(thinking_block)}c).",
                            flush=True,
                        )
                    else:
                        # Genuinely nothing from the model. Mark this so
                        # the empty-reply guard below can detect a stuck
                        # loop if we recurse without making progress.
                        full_reply = ""
                        self._empty_replies += 1

                print(f"[QUERY] Done. Chunks: {chunk_count}, Reply: {len(full_reply)} chars", flush=True)

                # Hard guard: if the model has now produced two or more
                # empty replies in a row, the cloud endpoint is wedged.
                # Stop recursing — finalise the turn with a clear error
                # so the user sees what happened instead of a hang.
                if self._empty_replies >= 2:
                    print(
                        f"[QUERY] ABORT: {empty_replies} consecutive empty "
                        f"replies from model. Bailing out of recursion.",
                        flush=True,
                    )
                    self._after(0, self._flush_chunks)
                    self._after(
                        0, self._say,
                        "The model returned no content multiple times in a "
                        "row. The cloud endpoint may be having issues. Try "
                        "again or switch models in Settings.",
                    )
                    return

                clean, calls = parse_tool_calls(full_reply)
                # Log what we found so dev-mode issues are debuggable
                if calls:
                    print(f"[QUERY] Detected {len(calls)} tool call(s): "
                          f"{[c['plugin']+'.'+c['command'] for c in calls]}",
                          flush=True)
                elif full_reply.strip():
                    # Heuristic: model claimed it did something but emitted no
                    # call. Refuse to finalize the lie — replace the reply
                    # with a clear system note, do NOT TTS it, and force
                    # one retry so the model can correct itself. If the
                    # model lies again, accept the second reply as-is
                    # (recursion-safe: we tag the next call with
                    # _claim_retry_used so we only retry once).
                    if _reply_claims_action_without_call(full_reply):
                        snippet = full_reply.replace("\n", " ")[:240]
                        print(f"[QUERY] WARNING: model claims action but "
                              f"emitted no tool call. Reply: {snippet!r}",
                              flush=True)
                        if self._claim_retry_used >= 1:
                            # Already retried once and the model lied again.
                            # Accept the second reply as-is and let the
                            # user see the truth (it'll be spoken and
                            # logged). Don't loop — just continue to the
                            # normal finalization code below.
                            print(f"[QUERY] Model lied again after retry; "
                                  f"accepting as-is.", flush=True)
                            # NOTE: _after() takes positional args;
                            # the final True becomes system=True on _display.
                            self._after(0, self._display, "System",
                                        "⚠ Model repeated a claim without "
                                        "a tool call. Showing the reply as-is.",
                                        True)
                            # (No return — falls through to the
                            # normal append + TTS finalization.)
                        else:
                            self._claim_retry_used += 1
                            # Record the lie in chat history (so the user sees
                            # what the model actually said) but replace the
                            # live reply text with a refusal note that won't
                            # be spoken aloud.
                            self.messages.append(
                                {"role": "assistant", "content": full_reply})
                            refusal = (
                                "⚠ I described taking an action but didn't "
                                "actually call a tool, so nothing was done. "
                                "Re-issuing with the proper tool_call format."
                            )
                            self._after(0, self._display, "System", refusal, True)
                            # Ask the model to try again with a real tool call.
                            self.messages.append({
                                "role": "system",
                                "content": (
                                    "Your previous reply claimed to save/edit/"
                                    "run something but contained no tool_call "
                                    "block. That action did NOT happen. You MUST "
                                    "either emit a proper tool_call now "
                                    "(SelfDevPlugin.propose_edit with "
                                    "create_if_missing=True for new files, "
                                    "or run_command on ApprovedShellPlugin "
                                    "for shell ops, etc.) or tell the user "
                                    "plainly that you cannot do it. Do not "
                                    "claim the file is saved unless you "
                                    "actually called a tool and the tool "
                                    "returned ok=true."
                                ),
                            })
                            self._save_chat_history()
                            # Re-flush the streamed chunks (the lie is already
                            # on screen from the streaming phase; the system
                            # note we just queued will appear beneath it).
                            self._after(0, self._flush_chunks)
                            # Skip TTS of the lie; speak a short status instead.
                            self._after(0, self._display, "System",
                                        "(refused — retrying)", system=True)
                            self._after(0, self._query_llm)
                            return
                if calls and tool_calls < max_tools:
                    tool_calls += 1
                    # Only append the assistant turn if it has content.
                    # An empty full_reply (model stream returned 0
                    # chars AND no thinking) adds nothing to the
                    # conversation — saving it just pollutes history
                    # and triggers the empty-reply loop on the next
                    # turn. The tool result below is what matters.
                    #
                    # IMPORTANT: if the reply was synthesized from the
                    # cloud model's thinking field (no content stream),
                    # do NOT save the raw reasoning here — it would
                    # poison the next turn. Save a short marker so the
                    # conversation still has continuity for the model,
                    # but history doesn't grow by 4KB of "I will wait
                    # for the system to provide the result" text.
                    if full_reply.strip() and not getattr(self,
                            "_used_thinking_fallback", False):
                        self.messages.append({"role": "assistant", "content": full_reply})
                    elif getattr(self, "_used_thinking_fallback", False):
                        marker = "[model emitted thinking only; tool call dispatched]"
                        self.messages.append({"role": "assistant", "content": marker})
                        self._used_thinking_fallback = False  # reset for next turn
                    for tc in calls:
                        self._after(0, self._display, "System", f"Tool: {tc['plugin']}.{tc['command']}", True)
                        # --- Auto-enable hint for SelfDevPlugin when off ---
                        if (tc.get("plugin") == "SelfDevPlugin"
                                and not bool(self.cfg.get("dev_mode_enabled", False))):
                            hint = (
                                "Dev mode is OFF. Click the 'Dev Mode: OFF' "
                                "button in the toolbar (or open Settings → "
                                "Self-Development Mode) to enable it, then "
                                "ask me again. The SelfDevPlugin refused this "
                                "request so I can keep you in control."
                            )
                            res = {"ok": False, "error": hint,
                                   "dev_mode_required": True}
                            self._after(0, self._display, "System", hint, True)
                        else:
                            res = self.plugin_manager.execute_command(
                                tc["plugin"], tc["command"], tc.get("args", {}))
                        # Format shell command results as readable text so
                        # the model can interpret them as actual command
                        # output. Other plugins keep their JSON structure.
                        if (tc["plugin"] == "ApprovedShellPlugin"
                                and tc["command"] == "run_command"
                                and isinstance(res, dict)):
                            if res.get("ok"):
                                out = res.get("stdout", "") or ""
                                err = res.get("stderr", "") or ""
                                ec = res.get("exit_code", 0)
                                content = out
                                if err:
                                    sep = "" if (content and content.endswith("\n")) else "\n"
                                    content = content + sep + err
                                if res.get("truncated"):
                                    content = (content.rstrip("\n") +
                                               "\n[output truncated]")
                                content = content + f"\n[exit {ec}]"
                            else:
                                err = res.get("error", "command failed")
                                if res.get("denied"):
                                    content = f"[denied by user] {err}"
                                else:
                                    out = res.get("stdout", "") or ""
                                    eout = res.get("stderr", "") or ""
                                    content = f"[command failed: {err}]"
                                    if out:
                                        content = content + f"\nstdout:\n{out}"
                                    if eout:
                                        content = content + f"\nstderr:\n{eout}"
                        else:
                            content = json.dumps(res)

                        # Send the result in a way that survives the round-trip
                        # to whatever model is on the other end. Three things
                        # matter:
                        #   (a) Ollama's /api/chat spec uses `tool_name` (not
                        #       `name`) on a role=tool message. We write both
                        #       so callers that read either still bind the
                        #       result to the prior tool call.
                        #   (b) Many chat models (especially chat-tuned /
                        #       instruct ones) only read content on
                        #       role=user/assistant/system. An orphan role=tool
                        #       message with no preceding structured
                        #       tool_calls on the assistant turn looks like
                        #       a dangling message to those models and is
                        #       effectively dropped from their context. So
                        #       we ALSO embed the result inside the
                        #       reinforcement user message that follows -
                        #       a model that ignores role=tool will still
                        #       see the data as part of a user turn.
                        #   (c) If the result is small we inline the full
                        #       content; if it is large we truncate to a
                        #       head/tail window so the context doesn't
                        #       blow up. The full content is still in
                        #       self.messages for the in-turn consumption
                        #       path; the user-facing copy is what we
                        #       hand to the model.
                        tool_call_label = f"{tc['plugin']}.{tc['command']}"
                        self.messages.append({
                            "role": "tool",
                            "name": tool_call_label,        # legacy / parser-side
                            "tool_name": tool_call_label,   # native Ollama spec
                            "content": content,
                        })
                        # Show the raw tool result to the user BEFORE the LLM
                        # gets a chance to hallucinate around it. This is a
                        # debug/transparency feature: if the plugin returns
                        # real data but the user still sees fake contents in
                        # the assistant reply, we know the LLM (not the
                        # plugin) is the problem.
                        try:
                            preview = content
                            if len(preview) > 2000:
                                preview = preview[:2000] + "...[truncated for display]"
                            self._after(0, self._display, "Tool Result",
                                        f"{tool_call_label}:\n{preview}",
                                        True)
                        except Exception:
                            pass
                        # Build the user-side reinforcement. We embed the
                        # actual result inline (truncated for size) so any
                        # model that ignores role=tool still gets the data
                        # in a role it will read. Without this, models that
                        # don't natively parse role=tool see the tool call
                        # happen but receive no result and either
                        # hallucinate one or refuse to answer.
                        INLINE_LIMIT = 6000
                        if isinstance(content, str) and len(content) > INLINE_LIMIT:
                            head = content[: INLINE_LIMIT // 2]
                            tail = content[-INLINE_LIMIT // 2 :]
                            inline = (
                                f"{head}\n"
                                f"... [truncated {len(content) - INLINE_LIMIT} chars] ...\n"
                                f"{tail}"
                            )
                        else:
                            inline = content if isinstance(content, str) else str(content)
                        self.messages.append({
                            "role": "user",
                            "content": (
                                f"[system note] The tool `{tool_call_label}` "
                                f"completed. Its actual output is below - "
                                f"quote or summarise it faithfully; do NOT "
                                f"invent different contents. If the result is "
                                f"an error, surface the error to the user "
                                f"verbatim.\n\n"
                                f"<tool_result name=\"{tool_call_label}\">\n"
                                f"{inline}\n"
                                f"</tool_result>"
                            ),
                        })
                        # If this tool result is a SelfDevPlugin apply that
                        # touched the running script, surface the restart offer.
                        try:
                            if (tc.get("plugin") == "SelfDevPlugin"
                                    and isinstance(res, dict)
                                    and res.get("restart_recommended")):
                                self._after(0, self._dev_restart_offer,
                                            res.get("path"))
                        except Exception:
                            pass
                    self._save_chat_history()
                    self._after(0, self._query_llm)
                    return

                # Final reply path (no tool calls). If the model
                # produced nothing, refuse to save an empty assistant
                # turn — it pollutes history. Finalise with a clear
                # message so the user knows what happened.
                if not full_reply.strip():
                    print(
                        f"[QUERY] Final reply empty (chunks={chunk_count}, "
                        f"thinking={len(thinking_block)}c). Not appending.",
                        flush=True,
                    )
                    self._after(0, self._flush_chunks)
                    self._after(
                        0, self._say,
                        "The model returned an empty response. Try again "
                        "or switch to a different model in Settings.",
                    )
                    return

                # If this final reply was synthesized from the cloud
                # model's thinking field (no content stream), do NOT
                # save the raw reasoning into history or TTS-read it
                # aloud — it would pollute the next turn and speak
                # 4KB of model musing. Save a short marker instead, and
                # surface a one-line system note so the user knows what
                # happened.
                if getattr(self, "_used_thinking_fallback", False):
                    tc = len(thinking_block)
                    msg = ("[model returned reasoning only ("
                           f" {tc} chars), no visible reply]")
                    # NOTE: _after() only takes positional args; the
                    # final `True` becomes system=True on _display.
                    self._after(0, self._display, "System", msg, True)
                    self.messages.append({"role": "assistant",
                                          "content": msg})
                    self._used_thinking_fallback = False
                    self._save_chat_history()
                    self._after(0, self._flush_chunks)
                    return
                self.messages.append({"role": "assistant", "content": full_reply})
                self._save_chat_history()
                self._after(0, self._flush_chunks)
                self._after(0, self._speak_reply, full_reply)
            except Exception as e:
                import traceback
                print(f"[QUERY] EXCEPTION: {e}", flush=True)
                traceback.print_exc()
                self._after(0, self._say, f"Query error: {e}")

        threading.Thread(target=do_query, daemon=True).start()

    def _speak_reply(self, full):
        # Reply body was already streamed into the chat via _display_chunk/_flush_chunks.
        # Emit only a small status marker (no body) so the user can see who is speaking
        # without duplicating the reply text.
        self._display("System", "(speaking)", system=True)
        self._set_status("Speaking...")
        self._set_head_state("SPEAKING")
        try: self.head.set_tts_text(full)
        except: pass
        self.tts.speak(full, on_end=lambda: self._after(0, self._on_speech_end))

    def _on_speech_end(self):
        self._set_status(f"Say '{self.wake_word}' to wake me up...")
        try:
            self.head.clear_tts_text()
            self.head.set_state("IDLE")
        except: pass

    def _interrupt(self):
        self.cancel_event.set()
        self.tts.stop()
        # Reset to the wake prompt right away so the status bar doesn't
        # get stuck on "Interrupted" if the voice listener was paused in
        # COMMAND_RECORD / ACTIVE_LISTEN and never naturally cycles back
        # to WAKE_LOOP. _on_speech_end would normally do this, but
        # tts.stop() (called here) bypasses the speak callback, so we
        # have to do it ourselves. Using the same string as the
        # wake-listener / _on_speech_end keeps the bar consistent.
        self._set_status(f"Say '{self.wake_word}' to wake me up...")
        try:
            self.head.clear_tts_text()
            self.head.set_state("IDLE")
        except: pass

    def _on_close(self):
        self._shutting_down = True
        self.cancel_event.set()
        if self.voice: self.voice.stop()
        self.tts.shutdown()
        self._save_chat_history()
        try: self.master.destroy()
        except: pass

    # ------------------------------------------------------------------ #
    # Approval flow (shared by ApprovedShellPlugin + SelfDevPlugin)
    # ------------------------------------------------------------------ #

    def _register_approval_modal(self, dlg, owner):
        """Track the active approval modal so duplicate requests can raise
        it instead of opening a new Toplevel (which is what causes
        approval to look "stuck" — the second modal ends up behind the
        first one and the worker thread waits until the 5-minute timeout).

        Race-safety: after a modal is closed, Tk processes its <Destroy>
        event lazily. If a new approval request comes in before the Tk
        event loop has run the destroy, winfo_exists() still reports True
        on the old widget. Without the extra check below, that causes
        every subsequent tool call to be silently denied with "another
        approval is already pending" — which is exactly the "tool ran
        but no modal appeared" symptom.
        """
        with self._approval_modal_lock:
            existing = self._approval_modal
            existing_owner = self._approval_modal_owner
            if existing is not None:
                # Probe the existing widget. If it no longer exists, or
                # it's been destroyed but the event hasn't drained yet,
                # clear the stale registration and let this one proceed.
                stale = False
                try:
                    if not existing.winfo_exists():
                        stale = True
                except Exception:
                    stale = True
                if not stale:
                    # Instead of refusing, destroy the existing modal and proceed
                    try:
                        existing.destroy()
                    except Exception:
                        pass
                    # fall through to register new modal
            self._approval_modal = dlg
            self._approval_modal_owner = owner
            return True, owner

    def _clear_approval_modal(self, dlg):
        with self._approval_modal_lock:
            if self._approval_modal is dlg:
                self._approval_modal = None
                self._approval_modal_owner = ""

    def _shell_approval_request(self, payload):
        """Called from a plugin's worker thread. Routes to the Tk main
        thread, blocks until the user decides, returns a decision dict.

        Both ApprovedShellPlugin and SelfDevPlugin use this callback.
        Payloads with kind=='shell_command' get the shell-style modal;
        everything else gets the dev-style modal.
        """
        result_box = {"value": None}
        done = threading.Event()

        def show():
            try:
                kind = payload.get("kind", "")
                if kind == "shell_command":
                    result_box["value"] = self._show_shell_approval(payload)
                else:
                    result_box["value"] = self._show_dev_approval(payload)
                    print(f"[DEBUG] _dev_approval_request show result: {result_box['value']}")
            except Exception as e:
                result_box["value"] = {"ok": False, "decision": "deny",
                                       "error": f"approval UI error: {e}"}
            finally:
                done.set()

        try:
            self.master.after(0, show)
        except Exception as e:
            return {"ok": False, "decision": "deny",
                    "error": f"failed to schedule approval UI: {e}"}

        # 90s instead of 5 min — long enough for the user to switch focus,
        # short enough that a forgotten modal doesn't burn the whole turn.
        if not done.wait(timeout=90):
            print("[DEBUG] _dev_approval_request timeout")
            return {"ok": False, "decision": "deny",
                    "error": "approval timed out (90s)"}
        return result_box["value"] or {"ok": False, "decision": "deny"}
        print(f"[DEBUG] _dev_approval_request returning {result_box['value']}")

    def _show_shell_approval(self, payload):
        """Modal for ApprovedShellPlugin.run_command.

        Shows cmd, cwd, timeout, reason; three buttons: Deny, Approve,
        Approve+sticky (when sticky_enabled). Returns:
            {"ok": True,  "decision": "approve"}
            {"ok": True,  "decision": "sticky"}
            {"ok": False, "decision": "deny"}
        """
        if self._shutting_down:
            return {"ok": False, "decision": "deny", "error": "shutting down"}
        if not self._tk_usable():
            return self._show_shell_approval_chat(payload)

        decision = {"ok": False, "decision": "deny"}
        parent = self.master
        dlg = tk.Toplevel(parent)
        dlg.title("Sepian: Approve Shell Command")
        dlg.configure(bg="#111111")
        dlg.transient(parent)
        dlg.resizable(True, True)
        # Register modal BEFORE raising so a duplicate request sees it.
        registered, _owner = self._register_approval_modal(dlg, "shell")
        if not registered:
            # Another approval modal is already on-screen; raise it and
            # refuse this one so we don't bury the existing prompt.
            dlg.destroy()
            return {"ok": False, "decision": "deny",
                    "error": "another approval is already pending — "
                             "approve or deny that one first"}
        # Make sure we clear the registration when this dialog goes away.
        dlg.bind("<Destroy>", lambda e: self._clear_approval_modal(dlg))
        # --- Robustly raise the modal so the user can actually see/click it.
        # On some WMs (notably Wayland) `transient` + `grab_set` alone leaves
        # the dialog hidden behind the main window, which makes it look like
        # approval is "stuck" until the 5-minute timeout fires.
        try:
            dlg.attributes("-topmost", True)
        except Exception:
            pass
        try:
            dlg.grab_set()
        except Exception:
            pass
        try:
            dlg.lift()
        except Exception:
            pass
        try:
            dlg.focus_force()
        except Exception:
            pass
        try:
            dlg.update_idletasks()
        except Exception:
            pass
        try:
            parent.update_idletasks()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = 720, 420
            dlg.geometry(f"{w}x{h}+{px + max(0,(pw-w)//2)}+{py + max(0,(ph-h)//3)}")
        except Exception:
            dlg.geometry("720x420")
        dlg.minsize(520, 320)
        # Clear topmost once the user has actually engaged with the dialog,
        # so it doesn't permanently cover other windows.
        def _release_topmost(_evt=None):
            try:
                dlg.attributes("-topmost", False)
            except Exception:
                pass
        try:
            dlg.bind("<FocusIn>", _release_topmost)
        except Exception:
            pass

        mono = font.Font(family="DejaVu Sans Mono", size=10)
        body = font.Font(family="Segoe UI", size=10)
        title = font.Font(family="Segoe UI", size=13, weight="bold")

        tk.Label(dlg,
                 text="The model wants to run a shell command.",
                 bg="#111111", fg="#ff6666", font=title).pack(pady=(14, 6))

        # Command box
        body_frame = tk.Frame(dlg, bg="#1a1a1a", bd=1, relief="sunken")
        body_frame.pack(fill="x", padx=14, pady=(2, 6))
        txt = scrolledtext.ScrolledText(body_frame, height=4,
                                        bg="#1a1a1a", fg="#e0e0e0",
                                        insertbackground="#e0e0e0",
                                        font=mono, wrap="word",
                                        relief="flat", bd=4)
        txt.pack(fill="x")
        txt.insert("1.0", payload.get("cmd", ""))
        txt.configure(state="disabled")

        # Metadata
        meta_parts = [f"cwd: {payload.get('cwd') or '(default)'}",
                      f"timeout: {payload.get('timeout', '?')}s"]
        if payload.get("reason"):
            meta_parts.append(f"reason: {payload['reason']}")
        tk.Label(dlg, text="   |   ".join(meta_parts),
                 bg="#111111", fg="#999999", font=body).pack(pady=(0, 8))

        tk.Label(dlg,
                 text="Approve with Enter, Deny with Escape.",
                 bg="#111111", fg="#777777", font=body).pack(pady=(0, 8))

        btn_frame = tk.Frame(dlg, bg="#111111")
        btn_frame.pack(side="bottom", fill="x", padx=14, pady=14)

        def choose(d):
            decision["decision"] = d
            decision["ok"] = (d in ("approve", "sticky"))
            try: dlg.grab_release()
            except Exception: pass
            dlg.destroy()

        # Look up plugin to know if sticky is enabled.
        sticky_on = False
        sticky_secs = 300
        try:
            shell = self.plugin_manager.get_plugin("ApprovedShellPlugin")
            if shell is not None:
                sticky_on = bool(shell.config.get("sticky_enabled", False))
                sticky_secs = int(shell.config.get("sticky_window_seconds", 300))
        except Exception:
            pass

        tk.Button(btn_frame, text="  Deny  ",
                  command=lambda: choose("deny"),
                  bg="#552222", fg="white", activebackground="#883333",
                  activeforeground="white", relief="flat", padx=14, pady=8,
                  font=body).pack(side="right", padx=(6, 0))
        sticky_label = (f"Approve + sticky ({sticky_secs}s)" if sticky_on
                        else "Approve + sticky (off)")
        tk.Button(btn_frame, text=sticky_label,
                  command=lambda: choose("sticky") if sticky_on else choose("approve"),
                  bg="#225522", fg="white", activebackground="#338833",
                  activeforeground="white", relief="flat", padx=14, pady=8,
                  font=body,
                  state=("normal" if sticky_on else "disabled")).pack(side="right", padx=(6, 0))
        tk.Button(btn_frame, text="  Approve (once)  ",
                  command=lambda: choose("approve"),
                  bg="#224488", fg="white", activebackground="#336699",
                  activeforeground="white", relief="flat", padx=14, pady=8,
                  font=body).pack(side="right", padx=(6, 0))

        dlg.bind("<Return>", lambda e: choose("approve"))
        dlg.bind("<Escape>", lambda e: choose("deny"))
        # Closing the dialog via the window manager X button counts as deny.
        try:
            dlg.protocol("WM_DELETE_WINDOW", lambda: choose("deny"))
        except Exception:
            pass

        try:
            parent.wait_window(dlg)
        except Exception:
            pass
        return decision

    def _show_shell_approval_chat(self, payload):
        """Headless fallback for shell commands: print the request and
        wait for the user to type /approve, /sticky, or /reject."""
        # If the user has explicitly disabled chat fallback, deny rather
        # than hang the worker thread waiting for /approve.
        if not bool(self.cfg.get("dev_chat_fallback_enabled", True)):
            # NOTE: _after() takes positional args; True -> system=True on _display.
            self._after(0, self._display, "System",
                        "Chat fallback approvals are disabled in Settings; "
                        "denying shell command. Enable "
                        "dev_chat_fallback_enabled to allow /approve.",
                        True)
            return {"ok": False, "decision": "deny",
                    "error": "chat fallback disabled in settings"}
        body = []
        body.append("[SHELL APPROVAL REQUIRED]")
        body.append(f"cmd: {payload.get('cmd')}")
        body.append(f"cwd: {payload.get('cwd') or '(default)'}")
        body.append(f"timeout: {payload.get('timeout')}s")
        if payload.get("reason"):
            body.append(f"reason: {payload['reason']}")
        body.append("Type /approve, /sticky, or /reject in chat to decide.")

        self._after(0, self._display, "System", "\n".join(body), True)

        result_box = {"value": None}
        done = threading.Event()
        if not hasattr(self, "_shell_chat_waiters"):
            self._shell_chat_waiters = {}
        self._shell_chat_waiters["shell"] = (done, result_box)

        if not done.wait(timeout=600):
            self._shell_chat_waiters.pop("shell", None)
            return {"ok": False, "decision": "deny",
                    "error": "shell approval timed out (10 min)"}
        return result_box["value"] or {"ok": False, "decision": "deny"}

    def _handle_shell_chat_command(self, verb):
        """Process /approve /sticky /reject for the pending shell approval."""
        if not hasattr(self, "_shell_chat_waiters"):
            return False
        entry = self._shell_chat_waiters.pop("shell", None)
        if not entry:
            return False
        # Honour dev_chat_fallback_enabled for the shell path too.
        if not bool(self.cfg.get("dev_chat_fallback_enabled", True)):
            if not self._tk_usable():
                self._display("System",
                              "Chat fallback approvals are disabled in "
                              "Settings.",
                              system=True)
                # Wake the waiter with a deny so it doesn't hang.
                done, result_box = entry
                result_box["value"] = {"ok": False, "decision": "deny",
                                       "error": "chat fallback disabled"}
                done.set()
                return True
        done, result_box = entry
        if verb == "/approve":
            result_box["value"] = {"ok": True, "decision": "approve"}
        elif verb == "/sticky":
            result_box["value"] = {"ok": True, "decision": "sticky"}
        else:
            result_box["value"] = {"ok": False, "decision": "deny"}
        done.set()
        self._after(0, self._display, "System",
                    f"Shell command {verb} -> {result_box['value']['decision']}",
                    True)  # system=True (positional)
        return True

    # ------------------------------------------------------------------ #
    # SelfDevPlugin approval flow
    # ------------------------------------------------------------------ #

    def _dev_approval_request(self, payload):
        """Called from the plugin's worker thread. Routes to the Tk main
        print(f"[DEBUG] _dev_approval_request payload: {payload}")
        thread, blocks (via threading.Event) until the user decides, and
        returns the decision dict."""
        result_box = {"value": None}
        done = threading.Event()

        def show():
            try:
                result_box["value"] = self._show_dev_approval(payload)
            except Exception as e:
                result_box["value"] = {"ok": False, "decision": "deny",
                                       "error": f"approval UI error: {e}"}
            finally:
                done.set()

        # We're on a worker thread — marshal to the Tk main thread.
        try:
            self.master.after(0, show)
        except Exception as e:
            return {"ok": False, "decision": "deny",
                    "error": f"failed to schedule approval UI: {e}"}

        # 90s instead of 5 min — see comment in _shell_approval_request.
        if not done.wait(timeout=90):
            return {"ok": False, "decision": "deny",
                    "error": "approval timed out (90s)"}
        return result_box["value"] or {"ok": False, "decision": "deny"}

    def _show_dev_approval(self, payload):
        """Build and run a Tk modal for the given payload. Returns a
        print(f"[DEBUG] _show_dev_approval called with kind={payload.get("kind")}")
        decision dict: {ok, decision: 'approve'|'deny', ...}.

        Payload kinds:
          - propose_edit / approve_edit: code diff
          - run_test: shell command
          - snapshot_restore: file list to restore
        """
        if self._shutting_down:
            return {"ok": False, "decision": "deny",
                    "error": "shutting down"}

        kind = payload.get("kind", "unknown")
        # Fallback: chat-only approval when tkinter isn't usable here.
        if not self._tk_usable():
            return self._show_dev_approval_chat(payload)

        decision = {"ok": False, "decision": "deny"}

        parent = self.master
        dlg = tk.Toplevel(parent)
        dlg.title(f"Sepian Dev Approval — {kind}")
        dlg.configure(bg="#111111")
        dlg.transient(parent)
        dlg.resizable(True, True)
        # Register modal BEFORE raising so a duplicate request sees it.
        registered, _owner = self._register_approval_modal(dlg, "dev")
        if not registered:
            dlg.destroy()
            return {"ok": False, "decision": "deny",
                    "error": "another approval is already pending — "
                             "approve or deny that one first"}
        dlg.bind("<Destroy>", lambda e: self._clear_approval_modal(dlg))
        # --- Robustly raise the modal so the user can actually see/click it.
        # On some WMs (Wayland in particular) transient+grab alone leaves
        # the dialog hidden behind the main window, which makes approval
        # look "stuck" until the 5-minute timeout fires.
        try:
            dlg.attributes("-topmost", True)
        except Exception:
            pass
        try:
            dlg.grab_set()
        except Exception:
            pass
        try:
            dlg.lift()
        except Exception:
            pass
        try:
            dlg.focus_force()
        except Exception:
            pass
        try:
            dlg.update_idletasks()
        except Exception:
            pass
        try:
            parent.update_idletasks()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = 780, 560
            dlg.geometry(f"{w}x{h}+{px + max(0,(pw-w)//2)}+{py + max(0,(ph-h)//3)}")
        except Exception:
            dlg.geometry("780x560")
        dlg.minsize(560, 380)
        def _release_topmost(_evt=None):
            try:
                dlg.attributes("-topmost", False)
            except Exception:
                pass
        try:
            dlg.bind("<FocusIn>", _release_topmost)
        except Exception:
            pass

        mono = font.Font(family="DejaVu Sans Mono", size=10)
        body = font.Font(family="Segoe UI", size=10)
        title = font.Font(family="Segoe UI", size=13, weight="bold")

        # Header
        header_text = {
            "propose_edit":     "Sepian wants to edit a file. Review the diff.",
            "propose_new_file": "Sepian wants to create a NEW file. Review the contents.",
            "approve_edit":     "Final approval: apply this edit?",
            "run_test":         "Sepian wants to run a test command.",
            "snapshot_restore": "Sepian wants to restore files from a snapshot.",
        }.get(kind, f"Sepian dev approval: {kind}")

        tk.Label(dlg, text=header_text, bg="#111111", fg="#ff6666",
                 font=title).pack(pady=(14, 6))

        # Metadata line
        meta_parts = []
        if kind in ("propose_edit", "propose_new_file", "approve_edit"):
            meta_parts.append(f"file: {payload.get('path','?')}")
            if payload.get("edit_id"):
                meta_parts.append(f"id: {payload['edit_id']}")
            if payload.get("rationale"):
                meta_parts.append(f"reason: {payload['rationale']}")
            if kind == "propose_new_file":
                size = len((payload.get("new_text") or "").encode("utf-8"))
                meta_parts.append(f"new file ({size} bytes)")
        elif kind == "run_test":
            meta_parts.append(f"cmd: {payload.get('cmd','?')}")
            if payload.get("cwd"):
                meta_parts.append(f"cwd: {payload['cwd']}")
            meta_parts.append(f"timeout: {payload.get('timeout','?')}s")
            if payload.get("reason"):
                meta_parts.append(f"reason: {payload['reason']}")
        elif kind == "snapshot_restore":
            meta_parts.append(f"snapshot: {payload.get('snapshot','?')}")
            files = payload.get("files") or []
            if files:
                meta_parts.append(f"files: {', '.join(files[:6])}"
                                  + ("..." if len(files) > 6 else ""))

        if meta_parts:
            tk.Label(dlg, text="   |   ".join(meta_parts),
                     bg="#111111", fg="#999999", font=body,
                     wraplength=740, justify="left").pack(pady=(0, 8))

        # Body — diff / new-file content / command / file list
        body_frame = tk.Frame(dlg, bg="#1a1a1a", bd=1, relief="sunken")
        body_frame.pack(fill="both", expand=True, padx=14, pady=(2, 8))
        txt = scrolledtext.ScrolledText(body_frame, bg="#1a1a1a", fg="#e0e0e0",
                                        insertbackground="#e0e0e0", font=mono,
                                        wrap="none", relief="flat", bd=4)
        txt.pack(fill="both", expand=True)
        if kind in ("propose_edit", "approve_edit"):
            txt.insert("1.0", payload.get("diff", "(no diff)"))
        elif kind == "propose_new_file":
            # Show the full proposed new file content (more useful than
            # the unified-diff-with-empty-base for a brand-new file).
            new_text = payload.get("new_text", "")
            label = f"# NEW FILE: {payload.get('path','?')}\n\n"
            txt.insert("1.0", label + new_text)
        elif kind == "run_test":
            argv = payload.get("argv") or []
            txt.insert("1.0", " ".join(argv) if argv else payload.get("cmd",""))
        elif kind == "snapshot_restore":
            txt.insert("1.0",
                       "\n".join(payload.get("files") or []) or "(empty)")
        # Tag added/removed lines for color
        try:
            txt.tag_config("add", foreground="#88ff88")
            txt.tag_config("rem", foreground="#ff8888")
            content = txt.get("1.0", "end").splitlines()
            for i, line in enumerate(content, start=1):
                if line.startswith("+") and not line.startswith("+++"):
                    txt.tag_add("add", f"{i}.0", f"{i}.end")
                elif line.startswith("-") and not line.startswith("---"):
                    txt.tag_add("rem", f"{i}.0", f"{i}.end")
        except Exception:
            pass
        txt.configure(state="disabled")

        # If the proposed edit touches sepianai.py, surface a warning
        running_script = "/home/davel/Public/Sepian-Unified-Workspace/sepianai.py"
        if kind in ("propose_edit", "approve_edit") and \
                payload.get("path") == running_script:
            tk.Label(dlg,
                     text="⚠ This edit will modify the running Sepian script. "
                          "Restart Sepian after applying.",
                     bg="#111111", fg="#ffcc66", font=body,
                     wraplength=740, justify="left").pack(pady=(0, 6))

        # Buttons
        btn_frame = tk.Frame(dlg, bg="#111111")
        btn_frame.pack(side="bottom", fill="x", padx=14, pady=14)

        def choose(d):
            decision["decision"] = d
            decision["ok"] = (d == "approve")
            try: dlg.grab_release()
            except Exception: pass
            dlg.destroy()

        tk.Button(btn_frame, text="  Deny  ",
                  command=lambda: choose("deny"),
                  bg="#552222", fg="white", activebackground="#883333",
                  activeforeground="white", relief="flat", padx=14, pady=8,
                  font=body).pack(side="right", padx=(6, 0))
        tk.Button(btn_frame, text="  Approve  ",
                  command=lambda: choose("approve"),
                  bg="#224488", fg="white", activebackground="#336699",
                  activeforeground="white", relief="flat", padx=14, pady=8,
                  font=body).pack(side="right", padx=(6, 0))

        dlg.bind("<Return>", lambda e: choose("approve"))
        dlg.bind("<Escape>", lambda e: choose("deny"))
        # Closing the dialog via the window manager X button counts as deny.
        try:
            dlg.protocol("WM_DELETE_WINDOW", lambda: choose("deny"))
        except Exception:
            pass

        try:
            parent.wait_window(dlg)
        except Exception:
            pass
        return decision

    def _tk_usable(self):
        """True if Tkinter is available and we are on the Tk main thread."""
        try:
            import threading as _t
            if self.master is None or self._shutting_down:
                return False
            # We must be on Tk's main thread, otherwise widget creation
            # and wait_window() are unsafe. Worker threads should fall
            # back to the chat-based approval flow.
            return _t.current_thread() is _t.main_thread()
        except Exception:
            return False

    def _show_dev_approval_chat(self, payload):
        """Headless fallback when no usable GUI: post the request in chat
        and block (via threading.Event) for the user to type
        /approve <id> or /reject <id> in the chat box."""
        # If the user has explicitly disabled chat fallback, deny rather
        # than hang the worker thread.
        if not bool(self.cfg.get("dev_chat_fallback_enabled", True)):
            # NOTE: _after() takes positional args; True -> system=True on _display.
            self._after(0, self._display, "System",
                        "Chat fallback approvals are disabled in Settings; "
                        "denying dev request.",
                        True)
            return {"ok": False, "decision": "deny",
                    "error": "chat fallback disabled in settings"}
        kind = payload.get("kind", "?")
        edit_id = payload.get("edit_id") or payload.get("run_id") or "?"
        # Render a structured pending block in the chat
        body = []
        body.append(f"[DEV APPROVAL REQUIRED — {kind} id={edit_id}]")
        if kind in ("propose_edit", "propose_new_file", "approve_edit"):
            body.append(f"path: {payload.get('path')}")
            if payload.get("rationale"):
                body.append(f"rationale: {payload['rationale']}")
            if kind == "propose_new_file":
                body.append("----- new file contents -----")
                body.append((payload.get("new_text") or "").rstrip())
                body.append("----- end new file -----")
            else:
                body.append("----- diff -----")
                body.append((payload.get("diff") or "").rstrip())
                body.append("----- end diff -----")
        elif kind == "run_test":
            body.append(f"cmd: {payload.get('cmd')}")
            body.append(f"cwd: {payload.get('cwd') or '(default)'}")
            body.append(f"timeout: {payload.get('timeout')}s")
        elif kind == "snapshot_restore":
            body.append(f"snapshot: {payload.get('snapshot')}")
            body.append("files: " + ", ".join(payload.get("files") or []))
        body.append(f"Type '/approve {edit_id}' or '/reject {edit_id}' in chat.")

        self._after(0, self._display, "System", "\n".join(body), True)

        result_box = {"value": None, "id": edit_id}
        done = threading.Event()

        # Register a one-shot waiter keyed on this id
        if not hasattr(self, "_dev_chat_waiters"):
            self._dev_chat_waiters = {}
        self._dev_chat_waiters[edit_id] = (done, result_box)

        if not done.wait(timeout=600):
            self._dev_chat_waiters.pop(edit_id, None)
            return {"ok": False, "decision": "deny",
                    "error": "chat approval timed out (10 min)"}
        return result_box["value"] or {"ok": False, "decision": "deny"}

    def _handle_dev_chat_command(self, text):
        """Called from _send when text starts with '/approve' or '/reject'."""
        if not text.startswith("/"):
            return False
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return False
        verb, edit_id = parts[0].lower(), parts[1].strip()
        if verb not in ("/approve", "/reject"):
            return False
        # Honour the dev_chat_fallback_enabled flag: if it's off and we're
        # in chat-fallback mode (no usable GUI), refuse the command rather
        # than silently approving a sensitive action.
        if not bool(self.cfg.get("dev_chat_fallback_enabled", True)):
            if not self._tk_usable():
                self._display("System",
                              "Chat fallback approvals are disabled in "
                              "Settings. Enable 'dev_chat_fallback_enabled' "
                              "or use the GUI modal.",
                              system=True)
                return True
        waiters = getattr(self, "_dev_chat_waiters", {})
        entry = waiters.pop(edit_id, None)
        if not entry:
            self._display("System",
                          f"No pending approval with id '{edit_id}'.",
                          system=True)
            return True
        done, result_box = entry
        decision = "approve" if verb == "/approve" else "deny"
        result_box["value"] = {"ok": decision == "approve",
                               "decision": decision}
        done.set()
        self._display("System", f"{verb} {edit_id} -> {decision}", system=True)
        return True

    def _dev_restart_offer(self, edited_path):
        """If the edited path is sepianai.py, ask the user if they want to
        relaunch (headless: via chat; modal: via Tk)."""
        running = "/home/davel/Public/Sepian-Unified-Workspace/sepianai.py"
        if edited_path != running:
            return
        msg = ("sepianai.py was modified. Restart Sepian to pick up changes.\n"
               "Type '/restart' to relaunch, or ignore to keep current process.")
        # NOTE: _after() only takes positional args; the final True
        # becomes system=True on _display.
        self._after(0, self._display, "System", msg, True)
        # Also light up the toolbar restart hint
        try:
            if getattr(self, "_restart_hint_label", None) is not None:
                self._after(0, lambda: self._restart_hint_label.config(
                    text="sepianai.py changed — click /restart"))
        except Exception:
            pass

    def _dev_restart(self):
        """Spawn a new Sepian process and exit the current one."""
        import subprocess as _sp
        script = "/home/davel/Public/Sepian-Unified-Workspace/sepianai.py"
        try:
            _sp.Popen([sys.executable, script],
                      cwd="/home/davel/Public/Sepian-Unified-Workspace",
                      start_new_session=True,
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL,
                      stdin=subprocess.DEVNULL)
            # NOTE: _after() takes positional args; True -> system=True on _display.
            self._after(0, self._display, "System",
                        "Relaunched Sepian. Exiting current process.", True)
            self._after(200, self._on_close)
        except Exception as e:
            # NOTE: _after() takes positional args; True -> system=True on _display.
            self._after(0, self._display, "System",
                        f"Failed to relaunch: {e}", True)

    # ------------------------------------------------------------------ #
    # Dev-mode toolbar / pending-approvals UI
    # ------------------------------------------------------------------ #

    def _dev_mode_button_label(self):
        on = bool(self.cfg.get("dev_mode_enabled", False))
        return "Dev Mode: ON" if on else "Dev Mode: OFF"

    def _refresh_dev_mode_button(self):
        if getattr(self, "_shutting_down", False):
            return
        try:
            self._dev_mode_btn.config(
                text=self._dev_mode_button_label(),
                bg=("#224488" if self.cfg.get("dev_mode_enabled")
                    else "#1a1a1a"),
                fg=("#ffffff" if self.cfg.get("dev_mode_enabled")
                    else "#cccccc"),
            )
        except Exception:
            pass

    def _toggle_dev_mode(self):
        new_state = not bool(self.cfg.get("dev_mode_enabled", False))
        self.cfg["dev_mode_enabled"] = new_state
        # Live-apply to the SelfDevPlugin
        try:
            if self._dev_plugin is not None:
                self._dev_plugin.enabled = new_state
                if hasattr(self._dev_plugin, "set_config"):
                    self._dev_plugin.set_config(
                        {"allowed_paths": self.cfg.get("dev_allowed_paths")
                                          or ["/home/davel/Public/Sepian-Unified-Workspace"]})
        except Exception as e:
            print(f"[Sepian] _toggle_dev_mode apply error: {e}", flush=True)
        # Rebuild the in-memory system prompt so the running model sees
        # the SelfDevPlugin workflow paragraph (it was only built once
        # at startup before). This makes the toggle honest.
        self._rebuild_dev_workflow_prompt()
        # Keep config.json in sync
        self._save_config()
        self._refresh_dev_mode_button()
        state = "ENABLED" if new_state else "disabled"
        # Light up the toolbar restart hint so the user knows the change
        # took effect for the next turn but a restart is the cleanest path.
        try:
            if getattr(self, "_restart_hint_label", None) is not None:
                self._restart_hint_label.config(
                    text=f"Dev Mode {state} — restart recommended for clean prompt")
        except Exception:
            pass
        self._say(f"SelfDevPlugin {state}. In-memory system prompt rebuilt; "
                  "a restart is recommended so the model sees the new "
                  "workflow from the very next turn.")

    def _rebuild_dev_workflow_prompt(self):
        """Re-derive the SelfDevPlugin workflow paragraph and prepend it
        to the live system prompt so toggling Dev Mode takes effect
        without a full restart."""
        try:
            base = self.cfg.get("system_prompt") or ""
            # Strip any existing workflow paragraph we previously appended.
            marker = "\n\nSEPIAN SELF-DEVELOPMENT (dev_mode is ENABLED):"
            cut = base.find(marker)
            if cut >= 0:
                base = base[:cut]
            self.system_prompt = base
            if bool(self.cfg.get("dev_mode_enabled", False)):
                self.system_prompt += (
                    "\n\nSEPIAN SELF-DEVELOPMENT (dev_mode is ENABLED):\n"
                    "You have a SelfDevPlugin that can read/search files in the\n"
                    "workspace, PROPOSE code edits, and run tests. Every write or\n"
                    "test requires the user to click Approve in a modal dialog\n"
                    "before it runs. NEVER bypass the approval flow.\n"
                    "Pick the right tool for the task (ROUTING):\n"
                    "  - 'Read/inspect/list/find files' -> SelfDevPlugin.list_files,\n"
                    "    read_file, or search_code.\n"
                    "  - 'Edit an EXISTING file (change contents, fix code)'\n"
                    "    -> SelfDevPlugin.propose_edit with a unique old_text anchor.\n"
                    "  - 'CREATE a new file (write a story, save notes, create a\n"
                    "    new script)' -> SelfDevPlugin.propose_edit with\n"
                    "    create_if_missing=True, old_text=\"\", new_text=<full contents>.\n"
                    "    This is the correct tool for 'write a fairy tale',\n"
                    "    'create a new file called X', etc.\n"
                    "  - 'Run a shell command, build, install, git, system ops'\n"
                    "    -> ApprovedShellPlugin.run_command (with approval).\n"
                    "  - 'Run a test' -> SelfDevPlugin.run_test (with approval).\n"
                    "  - 'Control smart-home / TV / lights' -> HomeAssistantPlugin,\n"
                    "    FireTVPlugin (no approval needed; safe operations).\n"
                    "Workflow for any WRITE (propose_edit or run_command):\n"
                    "  1. Investigate first with read_file / list_files if needed.\n"
                    "  2. Call the matching tool with EXACTLY one well-formed\n"
                    "     <tool_call>...</tool_call> block. Include a 'rationale'.\n"
                    "  3. After the tool returns an edit_id / approval-id, STOP\n"
                    "     and tell the user what you proposed. Do NOT call any\n"
                    "     other state-changing tool on your own.\n"
                    "  4. The user reviews in a modal and either clicks Approve\n"
                    "     or Deny. If approved, the plugin applies the change;\n"
                    "     if denied, the proposal is discarded.\n"
                    "  5. As a chat fallback, the user can type '/approve <id>'\n"
                    "     or '/reject <id>'.\n"
                    "If dev_mode is OFF and the user asks you to edit files,\n"
                    "tell them to click the 'Dev Mode' button in the toolbar\n"
                    "first, then re-issue the request. Never edit sepianai.py or\n"
                    "any file outside the configured allowed_paths. If you do\n"
                    "edit sepianai.py, the app will offer a /restart — do not\n"
                    "auto-restart, wait for the user to click it."
                )
        except Exception as e:
            print(f"[Sepian] _rebuild_dev_workflow_prompt error: {e}", flush=True)

    def _count_pending_dev(self):
        """Return the number of pending propose_edit items in SelfDevPlugin."""
        try:
            if self._dev_plugin is None or not self._dev_plugin.enabled:
                return 0
            res = self._dev_plugin.execute("list_pending", {})
            if isinstance(res, dict):
                return int(res.get("count", 0))
        except Exception:
            pass
        return 0

    def _count_pending_shell(self):
        """Return the number of active sticky shell approvals."""
        try:
            shell = self.plugin_manager.get_plugin("ApprovedShellPlugin")
            if shell is None:
                return 0
            sticky = shell.list_sticky() or {}
            return len(sticky)
        except Exception:
            return 0

    def _pending_button_label(self):
        n_dev = self._count_pending_dev()
        n_shell = self._count_pending_shell()
        total = n_dev + n_shell
        # If a modal is currently up, surface that — it explains why a
        # Pending click doesn't seem to do anything (the modal is the
        # click target, not Pending itself).
        modal_visible = self._approval_modal_visible()
        if modal_visible == "visible":
            return "Pending: ⚠ modal open"
        if modal_visible == "hidden":
            # Modal exists in the registration dict but is not actually
            # visible on screen (typical Wayland/X11 bug: buried under
            # the main window). This is the "stuck pending" symptom.
            return "Pending: ⚠ MODAL HIDDEN"
        if total <= 0:
            return "Pending: 0"
        parts = []
        if n_dev:
            parts.append(f"{n_dev} edit")
        if n_shell:
            parts.append(f"{n_shell} sticky")
        return f"Pending: {total} ({' + '.join(parts)})"

    def _approval_modal_visible(self):
        """Return 'visible', 'hidden', or 'none' for the current approval modal.

        'hidden' is the dangerous state: a modal is registered but the user
        can't see it, so every subsequent approval request gets dedupe-rejected
        and times out at 90s. The Pending badge uses this to switch into a
        red 'MODAL HIDDEN' state and the button click raises the modal.
        """
        try:
            with self._approval_modal_lock:
                dlg = self._approval_modal
            if dlg is None:
                return "none"
            try:
                if not dlg.winfo_exists():
                    # Stale registration — clean it up.
                    self._clear_approval_modal(dlg)
                    return "none"
            except Exception:
                return "none"
            # winfo_viewable() returns 0 if the window is mapped but not
            # actually displayed (offscreen, iconified, or buried behind
            # another transient parent on WMs that ignore -topmost).
            try:
                viewable = bool(dlg.winfo_viewable())
            except Exception:
                viewable = True
            # Also check if it's actually on a visible screen region. A
            # window can be viewable but at coordinates off the desktop,
            # which is the classic Wayland "stuck behind main window" bug.
            try:
                dlg.update_idletasks()
                x = dlg.winfo_rootx()
                y = dlg.winfo_rooty()
                w = dlg.winfo_width()
                h = dlg.winfo_height()
                sw = dlg.winfo_screenwidth()
                sh = dlg.winfo_screenheight()
                on_screen = (w > 0 and h > 0 and
                             x + w > 0 and y + h > 0 and
                             x < sw and y < sh)
            except Exception:
                on_screen = True
            return "visible" if (viewable and on_screen) else "hidden"
        except Exception:
            return "none"

    def _raise_pending_modal(self):
        """Bring the registered approval modal to the front.

        Used when the badge detects the modal is hidden. This is the
        single most common cause of the "stuck pending" symptom.
        """
        try:
            with self._approval_modal_lock:
                dlg = self._approval_modal
            if dlg is None:
                self._say("No approval modal to raise.")
                return
            try:
                if not dlg.winfo_exists():
                    self._clear_approval_modal(dlg)
                    self._say("Modal was already gone.")
                    self._refresh_pending_badge()
                    return
            except Exception:
                pass
            try:
                dlg.attributes("-topmost", True)
            except Exception:
                pass
            try:
                dlg.deiconify()
            except Exception:
                pass
            try:
                dlg.lift()
            except Exception:
                pass
            try:
                dlg.focus_force()
            except Exception:
                pass
            try:
                dlg.grab_set()
            except Exception:
                pass
            try:
                # Re-center over the main window in case it drifted offscreen.
                parent = self.master
                parent.update_idletasks()
                dlg.update_idletasks()
                px, py = parent.winfo_rootx(), parent.winfo_rooty()
                pw, ph = parent.winfo_width(), parent.winfo_height()
                w = max(dlg.winfo_width(), 720)
                h = max(dlg.winfo_height(), 420)
                dlg.geometry(f"{w}x{h}+{px + max(0,(pw-w)//2)}"
                             f"+{py + max(0,(ph-h)//3)}")
            except Exception:
                pass
            self._say("Approval modal raised — click Approve or Deny.")
        except Exception as e:
            self._say(f"Could not raise modal: {e}")
        finally:
            self._refresh_pending_badge()

    def _refresh_pending_badge(self):
        """Periodically refresh the pending-approvals badge (main thread)."""
        if getattr(self, "_shutting_down", False):
            return
        try:
            label = self._pending_button_label()
            n_total = (self._count_pending_dev() + self._count_pending_shell())
            modal_visible = self._approval_modal_visible()
            if modal_visible == "hidden":
                # Red flashing-style background: an approval modal is
                # registered but not actually visible on screen. Clicking
                # the badge will raise it. This is the "stuck pending"
                # symptom — without this state the user has no signal
                # that something is waiting on them.
                bg, fg = "#cc2222", "#ffffff"
            elif modal_visible == "visible":
                # Bright yellow = "an approval modal is the actual click
                # target right now; Pending itself is a viewer".
                bg, fg = "#aa8800", "#ffffff"
            elif n_total > 0:
                bg, fg = "#882222", "#ffffff"
            else:
                bg, fg = "#1a1a1a", "#cccccc"
            self._pending_btn.config(text=label, bg=bg, fg=fg)
        except Exception:
            pass
        try:
            self.master.after(1500, self._refresh_pending_badge)
        except Exception:
            pass

    def _shell_quick_list(self):
        """Fire a synchronous ApprovedShellPlugin.list_approvals to show the
        approval flow works end-to-end. No actual shell command runs."""
        try:
            shell = self.plugin_manager.get_plugin("ApprovedShellPlugin")
            if shell is None:
                self._say("ApprovedShellPlugin not loaded.")
                return
            res = shell.execute("list_approvals", {})
            sticky = (res or {}).get("sticky_approvals") or {}
            if not sticky:
                self._say("ApprovedShellPlugin loaded. "
                          "0 active sticky approvals. The flow is working "
                          "— when the model asks to run a command, you'll "
                          "get a modal Approve/Deny prompt.")
            else:
                msg = f"ApprovedShellPlugin loaded. {len(sticky)} sticky " \
                      f"approval(s) active:\n" + \
                      "\n".join(f"  {k} ({v:.0f}s left)" for k, v in sticky.items())
                self._say(msg)
        except Exception as e:
            self._say(f"Shell quick-test error: {e}")

    def _show_pending_approvals(self):
        """Open a dialog listing all pending dev edits and active shell
        sticky approvals, with Approve / Reject / Revoke buttons."""
        if self._shutting_down:
            return
        # Gather state
        dev_pending = []
        try:
            if self._dev_plugin is not None and self._dev_plugin.enabled:
                r = self._dev_plugin.execute("list_pending", {})
                if isinstance(r, dict):
                    dev_pending = r.get("pending") or []
        except Exception as e:
            self._say(f"Error listing pending dev edits: {e}")
        shell_sticky = {}
        try:
            shell = self.plugin_manager.get_plugin("ApprovedShellPlugin")
            if shell is not None:
                shell_sticky = shell.list_sticky() or {}
        except Exception as e:
            self._say(f"Error listing shell sticky: {e}")

        if not dev_pending and not shell_sticky:
            self._say("No pending approvals.")
            return

        parent = self.master
        dlg = tk.Toplevel(parent)
        dlg.title("Pending Approvals")
        dlg.configure(bg="#111111")
        dlg.transient(parent)
        try:
            parent.update_idletasks()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = 820, 560
            dlg.geometry(f"{w}x{h}+{px + max(0,(pw-w)//2)}+{py + max(0,(ph-h)//3)}")
        except Exception:
            dlg.geometry("820x560")
        dlg.minsize(640, 400)

        mono = font.Font(family="DejaVu Sans Mono", size=10)
        body = font.Font(family="Segoe UI", size=10)
        title_f = font.Font(family="Segoe UI", size=13, weight="bold")

        tk.Label(dlg, text="Pending Approvals", bg="#111111", fg="#ff6666",
                 font=title_f).pack(pady=(12, 6))

        nb = ttk.Notebook(dlg)
        nb.pack(fill="both", expand=True, padx=12, pady=(2, 8))

        # ---- Dev tab ----
        dev_tab = ttk.Frame(nb)
        nb.add(dev_tab, text=f"Dev edits ({len(dev_pending)})")
        if not dev_pending:
            tk.Label(dev_tab, text="(no pending propose_edit items)",
                     bg="#111111", fg="#888888", font=body).pack(pady=20)
        else:
            for entry in dev_pending:
                self._build_pending_dev_row(dev_tab, entry, dlg, body, mono)

        # ---- Shell tab ----
        shell_tab = ttk.Frame(nb)
        nb.add(shell_tab, text=f"Shell sticky ({len(shell_sticky)})")
        if not shell_sticky:
            tk.Label(shell_tab, text="(no active sticky shell approvals)",
                     bg="#111111", fg="#888888", font=body).pack(pady=20)
        else:
            self._build_pending_shell_rows(shell_tab, shell_sticky, dlg, body, mono)

        # Close button
        ttk.Button(dlg, text="Close", command=dlg.destroy).pack(
            side="bottom", pady=(0, 12))

        # When the modal closes, refresh the badge
        dlg.bind("<Destroy>", lambda e: self._refresh_pending_badge())

    def _build_pending_dev_row(self, parent, entry, dlg, body, mono):
        edit_id = entry.get("edit_id", "?")
        path = entry.get("path", "?")
        rationale = entry.get("rationale", "")
        diff_lines = entry.get("diff_lines", 0)

        row = tk.Frame(parent, bg="#1a1a1a", bd=1, relief="ridge")
        row.pack(fill="x", padx=4, pady=4)

        hdr = tk.Frame(row, bg="#1a1a1a")
        hdr.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(hdr, text=f"id: {edit_id}", bg="#1a1a1a", fg="#ffcc66",
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Label(hdr, text=f"   {path}   ({diff_lines} diff lines)",
                 bg="#1a1a1a", fg="#cccccc", font=body).pack(side="left")
        if rationale:
            tk.Label(row, text=f"rationale: {rationale}", bg="#1a1a1a",
                     fg="#aaaaaa", font=body, wraplength=760,
                     justify="left").pack(anchor="w", padx=8, pady=(0, 4))

        btns = tk.Frame(row, bg="#1a1a1a")
        btns.pack(fill="x", padx=8, pady=(0, 8))

        def _fetch_pending_payload():
            """Pull the live payload dict (path, diff, ...) from the plugin."""
            try:
                with self._dev_plugin._pending_lock:
                    return self._dev_plugin._pending.get(edit_id)
            except Exception:
                return None

        def approve():
            # Close Pending first so the approval modal has a clean parent
            # (avoids the previous "stuck behind another Toplevel" symptom).
            # Then re-open the SAME diff in the real approve modal; the
            # modal's Approve button is the one that actually approves.
            #
            # We use apply_pending (NOT approve_edit) once the user clicks
            # Approve inside the modal, because approve_edit would open a
            # SECOND modal that's hidden by the first one — which is the
            # classic "stuck until 5-min timeout" symptom we are fixing.
            payload = _fetch_pending_payload()
            if not payload:
                messagebox.showinfo("Approve",
                                    "This edit is no longer pending "
                                    "(already approved or rejected).",
                                    parent=dlg)
                dlg.destroy()
                self._show_pending_approvals()
                self._refresh_pending_badge()
                return
            # Clear registration BEFORE destroying so the new modal can
            # register itself without colliding with the dedupe lock.
            self._clear_approval_modal(dlg)
            try:
                dlg.destroy()
                # Pump the event loop once so the destroy is fully processed
                # before we open the new modal — otherwise some WMs (Wayland
                # especially) leave the new modal visually buried under the
                # just-destroyed parent's transient chain.
                parent.update_idletasks()
            except Exception:
                pass
            decision = self._show_dev_approval({
                "kind": "approve_edit",
                "edit_id": edit_id,
                "path": payload.get("path"),
                "rationale": payload.get("rationale"),
                "diff": payload.get("diff"),
                "new_text": payload.get("new_text"),
                "is_new_file": payload.get("is_new_file"),
                "session": payload.get("session"),
            })
            approved = (isinstance(decision, dict)
                        and decision.get("decision") == "approve")
            if approved and self._dev_plugin is not None:
                # User clicked Approve inside the modal — apply directly,
                # bypassing the redundant inner _request_approval.
                res = self._dev_plugin.execute("apply_pending",
                                               {"edit_id": edit_id})
                if res.get("ok"):
                    self._display("System",
                                  f"Approved edit {edit_id} ({path})",
                                  system=True)
                    if res.get("restart_recommended"):
                        self._dev_restart_offer(res.get("path"))
                else:
                    self._display("System",
                                  f"approve_edit failed: {res.get('error')}",
                                  system=True)
            elif isinstance(decision, dict) and decision.get("decision") == "deny":
                # User denied — drop the entry from the plugin queue so
                # the model doesn't see it as still pending.
                if self._dev_plugin is not None:
                    self._dev_plugin.execute("reject_edit",
                                             {"edit_id": edit_id})
                self._display("System",
                              f"Denied edit {edit_id}",
                              system=True)
            else:
                err = (decision or {}).get("error", "") if isinstance(decision, dict) else ""
                if err:
                    self._display("System",
                                  f"Approval not granted: {err}",
                                  system=True)
            self._show_pending_approvals()
            self._refresh_pending_badge()

        def reject():
            if self._dev_plugin is None:
                return
            res = self._dev_plugin.execute("reject_edit", {"edit_id": edit_id})
            self._display("System",
                          f"Rejected edit {edit_id}: "
                          f"{res.get('ok') and 'ok' or res.get('error')}",
                          system=True)
            dlg.destroy()
            self._show_pending_approvals()
            self._refresh_pending_badge()

        def view_diff():
            # Read-only viewer. Opens the diff modal; clicking Approve
            # inside it applies via apply_pending (no second modal).
            payload = _fetch_pending_payload()
            if not payload:
                messagebox.showinfo("Diff", "(no diff available)",
                                    parent=dlg)
                return
            try:
                dlg.destroy()
            except Exception:
                pass
            decision = self._show_dev_approval({
                "kind": "approve_edit",
                "edit_id": edit_id,
                "path": payload.get("path"),
                "rationale": payload.get("rationale"),
                "diff": payload.get("diff"),
                "new_text": payload.get("new_text"),
                "is_new_file": payload.get("is_new_file"),
                "session": payload.get("session"),
            })
            approved = (isinstance(decision, dict)
                        and decision.get("decision") == "approve")
            if approved and self._dev_plugin is not None:
                res = self._dev_plugin.execute("apply_pending",
                                               {"edit_id": edit_id})
                if res.get("ok"):
                    self._display("System",
                                  f"Approved edit {edit_id} ({path})",
                                  system=True)
                    if res.get("restart_recommended"):
                        self._dev_restart_offer(res.get("path"))
                else:
                    self._display("System",
                                  f"approve_edit failed: {res.get('error')}",
                                  system=True)
            elif isinstance(decision, dict) and decision.get("decision") == "deny":
                if self._dev_plugin is not None:
                    self._dev_plugin.execute("reject_edit",
                                             {"edit_id": edit_id})
                self._display("System", f"Denied edit {edit_id}",
                              system=True)
            self._show_pending_approvals()
            self._refresh_pending_badge()

        tk.Button(btns, text="View diff", command=view_diff,
                  bg="#333333", fg="white", activebackground="#555555",
                  activeforeground="white", relief="flat", padx=10, pady=4,
                  font=body).pack(side="right", padx=(4, 0))
        tk.Button(btns, text="Reject", command=reject,
                  bg="#552222", fg="white", activebackground="#883333",
                  activeforeground="white", relief="flat", padx=10, pady=4,
                  font=body).pack(side="right", padx=(4, 0))
        tk.Button(btns, text="Approve", command=approve,
                  bg="#224488", fg="white", activebackground="#336699",
                  activeforeground="white", relief="flat", padx=10, pady=4,
                  font=body).pack(side="right", padx=(4, 0))

    def _build_pending_shell_rows(self, parent, sticky_map, dlg, body, mono):
        shell = self.plugin_manager.get_plugin("ApprovedShellPlugin")
        if shell is None:
            return
        for key, secs_left in sticky_map.items():
            row = tk.Frame(parent, bg="#1a1a1a", bd=1, relief="ridge")
            row.pack(fill="x", padx=4, pady=4)
            hdr = tk.Frame(row, bg="#1a1a1a")
            hdr.pack(fill="x", padx=8, pady=(6, 2))
            tk.Label(hdr, text=key, bg="#1a1a1a", fg="#cccccc",
                     font=mono).pack(side="left")
            tk.Label(hdr, text=f"   {max(0, secs_left):.0f}s left",
                     bg="#1a1a1a", fg="#88ff88",
                     font=body).pack(side="left")
            btns = tk.Frame(row, bg="#1a1a1a")
            btns.pack(fill="x", padx=8, pady=(0, 8))

            def revoke():
                # Easiest path: clear all stickies, then re-add the others
                # by re-issuing approvals. To keep it simple, just clear
                # this one key.
                try:
                    with shell._sticky_lock:
                        shell._sticky.pop(key, None)
                    self._display("System", f"Revoked sticky: {key}",
                                  system=True)
                except Exception as e:
                    self._display("System", f"Revoke error: {e}", system=True)
                dlg.destroy()
                self._show_pending_approvals()
                self._refresh_pending_badge()

            tk.Button(btns, text="Revoke", command=revoke,
                      bg="#552222", fg="white", activebackground="#883333",
                      activeforeground="white", relief="flat", padx=10, pady=4,
                      font=body).pack(side="right")

if __name__ == "__main__":
    root = tk.Tk()
    root.title("Sepian AI Assistant")
    root.geometry("1100x900")
    app = SepianApp(root)
    try: root.mainloop()
    except KeyboardInterrupt: app._on_close()
