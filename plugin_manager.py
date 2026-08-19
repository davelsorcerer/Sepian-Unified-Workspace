#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
plugin_manager.py - Manages loading and executing Sepian plugins

Fixes vs original:
* register() is now idempotent — duplicate calls are a no-op.
* load_from_directory() skips plugins already registered by name.
* No more double "[Plugin] Loaded:" log lines.
"""

import os
import sys
import json
import importlib
import inspect
from typing import Dict, List, Optional
from sepian_plugin import SepianPlugin


class PluginManager:
    """Manages the plugin ecosystem"""

    def __init__(self, plugin_dir="plugins", config_file="plugin_config.json"):
        self.plugins: Dict[str, SepianPlugin] = {}
        self.plugin_dir = plugin_dir
        self.config_file = config_file
        self.status_callback = None

    def register(self, plugin: SepianPlugin):
        """Register a plugin instance (idempotent)."""
        if plugin.name in self.plugins:
            existing = self.plugins[plugin.name]
            if existing is plugin:
                return  # exact same instance, nothing to do
            print(f"[Plugin] {plugin.name} already registered, "
                  f"ignoring duplicate (existing: {id(existing):#x}, "
                  f"new: {id(plugin):#x})")
            return
        self.plugins[plugin.name] = plugin
        plugin.status_callback = self._on_plugin_status
        print(f"[Plugin] Loaded: {plugin.name} - {plugin.get_description()}")

    def unregister(self, name: str):
        """Remove a plugin"""
        if name in self.plugins:
            del self.plugins[name]
            print(f"[Plugin] Unloaded: {name}")

    def load_from_directory(self):
        """Load all plugins from the plugins/ directory (skips already-registered)."""
        if not os.path.exists(self.plugin_dir):
            os.makedirs(self.plugin_dir)
            return

        # Add plugins directory to path
        parent_dir = os.path.dirname(os.path.abspath(self.plugin_dir))
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)

        for filename in os.listdir(self.plugin_dir):
            if filename.endswith('_plugin.py') and not filename.startswith('_'):
                module_name = filename[:-3]
                try:
                    module = importlib.import_module(
                        f"{self.plugin_dir}.{module_name}")
                    # Find plugin class in module
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        if (issubclass(obj, SepianPlugin) and
                                obj != SepianPlugin and
                                obj.__module__ == module.__name__):
                            # Instantiate just to read .name
                            temp = obj()
                            if temp.name in self.plugins:
                                print(f"[Plugin] {temp.name} already "
                                      f"registered, skipping {filename}")
                                break
                            self.register(temp)
                            break
                except Exception as e:
                    print(f"[Plugin] Failed to load {filename}: {e}")

    def get_plugin(self, name: str) -> Optional[SepianPlugin]:
        """Get a specific plugin"""
        return self.plugins.get(name)

    def get_all_plugins(self) -> List[SepianPlugin]:
        """Get all registered plugins"""
        return list(self.plugins.values())

    def get_all_commands(self) -> List[str]:
        """Get all available commands as 'plugin.command' format"""
        commands = []
        for plugin in self.plugins.values():
            for cmd in plugin.get_commands():
                commands.append(f"{plugin.name}.{cmd}")
        return commands

    def execute_command(self, plugin_name: str, command: str,
                        args: Dict) -> Dict:
        """Execute a command on a specific plugin.

        Plugin-name lookup is case-insensitive and tolerant of common
        variations (e.g. 'self_dev_plugin' -> 'SelfDevPlugin',
        'ApprovedShell' -> 'ApprovedShellPlugin') so the model doesn't
        have to remember exact names.

        If plugin_name is empty, we'll look up the plugin that
        provides the given command (some cloud models emit
        {"name": "read_file", "arguments": {...}} without a plugin
        prefix). This makes the system robust to that format.
        """
        if not command:
            return {"ok": False, "error": "missing command name"}

        plugin = None
        if plugin_name:
            plugin = self.get_plugin(plugin_name)
            if not plugin:
                # Try case-insensitive and suffix-tolerant match
                target = plugin_name.lower().replace("_", "").replace(" ", "")
                for p in self.plugins.values():
                    pn = p.name.lower().replace("_", "").replace(" ", "")
                    if pn == target or pn.startswith(target) or target.startswith(pn):
                        plugin = p
                        print(f"[Plugin] Resolved '{plugin_name}' -> "
                              f"'{p.name}'", flush=True)
                        break
        else:
            # No plugin name — find any plugin that exposes this
            # command. Prefer plugins whose name contains a substring
            # of the command (e.g. "read_file" → "SelfDevPlugin" which
            # has read_file), and only match exactly one plugin to
            # avoid ambiguity.
            matches = []
            cmd_lower = command.lower()
            for p in self.plugins.values():
                try:
                    cmds = p.get_commands()
                except Exception:
                    continue
                for c in cmds:
                    if c == command:
                        matches.append((p, c, 1.0))
                    elif c.lower() == cmd_lower:
                        matches.append((p, c, 0.9))
            if len(matches) == 1:
                plugin, _, _ = matches[0]
                print(f"[Plugin] Resolved bare command '{command}' -> "
                      f"'{plugin.name}'", flush=True)
            elif len(matches) > 1:
                # Ambiguous — pick the first plugin alphabetically to
                # keep things deterministic.
                matches.sort(key=lambda m: m[0].name)
                plugin, _, _ = matches[0]
                print(f"[Plugin] Bare command '{command}' matched "
                      f"{len(matches)} plugins; using '{plugin.name}'",
                      flush=True)
        if not plugin:
            available = ", ".join(sorted(self.plugins.keys()))
            return {"ok": False,
                    "error": (f"Plugin '{plugin_name}' not found "
                              f"(or no plugin exposes command "
                              f"'{command}'). Available: {available}")}

        if not plugin.enabled:
            return {"ok": False, "error": f"Plugin '{plugin.name}' is disabled"}

        try:
            result = plugin.execute(command, args)
            return result
        except Exception as e:
            return {"ok": False, "error": f"Execution error: {e}"}

    def try_voice_command(self, text: str) -> Optional[str]:
        """Try to handle a voice command with any plugin"""
        for plugin in self.plugins.values():
            if plugin.enabled:
                try:
                    result = plugin.handle_voice_command(text)
                    if result is not None:
                        return result
                except Exception as e:
                    print(f"[Plugin {plugin.name}] Voice handler error: {e}")
        return None

    def enable(self, name: str):
        """Enable a plugin"""
        if name in self.plugins:
            self.plugins[name].enabled = True

    def disable(self, name: str):
        """Disable a plugin"""
        if name in self.plugins:
            self.plugins[name].enabled = False

    def save_config(self):
        """Save all plugin configs"""
        config = {}
        for name, plugin in self.plugins.items():
            config[name] = {
                "enabled": plugin.enabled,
                "config": plugin.config
            }
        try:
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            print(f"[Plugin Manager] Failed to save config: {e}")

    def load_config(self):
        """Load plugin configs"""
        if not os.path.exists(self.config_file):
            return
        try:
            with open(self.config_file) as f:
                config = json.load(f)
            for name, data in config.items():
                if name in self.plugins:
                    self.plugins[name].enabled = data.get("enabled", True)
                    self.plugins[name].set_config(data.get("config", {}))
        except Exception as e:
            print(f"[Plugin Manager] Failed to load config: {e}")

    def set_status_callback(self, callback):
        """Set callback for plugin status updates"""
        self.status_callback = callback

    def _on_plugin_status(self, plugin_name: str, message: str):
        if self.status_callback:
            self.status_callback(plugin_name, message)

    def get_status_report(self) -> str:
        """Get a formatted status report of all plugins"""
        lines = ["=== Plugin Status ==="]
        for plugin in self.plugins.values():
            status = "✓" if plugin.enabled else "✗"
            lines.append(
                f"{status} {plugin.name}: {plugin.get_description()}")
        return "\n".join(lines)
