#!/usr/bin/env python3
"""
plugins/firetv_plugin.py - Amazon Fire TV control via ADB
Requires: adb (sudo apt install adb)
Enable ADB Debugging on Fire TV: Settings > My Fire TV > Developer Options > ADB Debugging ON
"""
import subprocess
from sepian_plugin import SepianPlugin


class FireTVPlugin(SepianPlugin):
    
    def get_description(self) -> str:
        return "Amazon Fire TV control via ADB"
    
    def get_commands(self) -> list:
        return [
            "home", "power", "up", "down", "left", "right",
            "select", "back", "menu", "volume_up", "volume_down", "mute",
            "launch_netflix", "launch_youtube", "launch_prime",
            "launch_disney", "launch_hulu", "launch_plex",
            "type", "connect", "disconnect"
        ]
    
    def get_default_config(self) -> dict:
        return {
            "ip": "",
            "auto_connect": True
        }
    
    def on_config_update(self):
        self.ip = self.config.get("ip", "")
        self.auto_connect = self.config.get("auto_connect", True)
        if self.ip and self.auto_connect:
            self._connect()
    
    def _connect(self) -> bool:
        """Connect to Fire TV via ADB"""
        try:
            result = subprocess.run(
                ["adb", "connect", f"{self.ip}:5555"],
                capture_output=True, text=True, timeout=5
            )
            success = "connected" in result.stdout.lower() or result.returncode == 0
            if success:
                self.notify_status(f"Connected to Fire TV at {self.ip}")
            return success
        except FileNotFoundError:
            self.notify_status("adb not installed. Run: sudo apt install adb")
            return False
        except Exception as e:
            self.notify_status(f"Connection failed: {e}")
            return False
    
    def _disconnect(self) -> bool:
        """Disconnect from Fire TV"""
        try:
            subprocess.run(["adb", "disconnect"], capture_output=True, timeout=5)
            self.notify_status("Disconnected from Fire TV")
            return True
        except:
            return False
    
    def execute(self, command: str, args: dict) -> dict:
        if not self.ip:
            return {"ok": False, "error": "Fire TV IP not set. Configure in plugin settings."}
        
        try:
            # Navigation commands
            nav_commands = {
                "home": "KEYCODE_HOME",
                "power": "KEYCODE_POWER",
                "up": "KEYCODE_DPAD_UP",
                "down": "KEYCODE_DPAD_DOWN",
                "left": "KEYCODE_DPAD_LEFT",
                "right": "KEYCODE_DPAD_RIGHT",
                "select": "KEYCODE_ENTER",
                "enter": "KEYCODE_ENTER",
                "back": "KEYCODE_BACK",
                "menu": "KEYCODE_MENU",
                "volume_up": "KEYCODE_VOLUME_UP",
                "volume_down": "KEYCODE_VOLUME_DOWN",
                "mute": "KEYCODE_VOLUME_MUTE",
            }
            
            if command in nav_commands:
                return self._adb_shell(["input", "keyevent", nav_commands[command]])
            
            # App launch commands.
            # YouTube on Fire TV devices uses Amazon's wrapper package
            # (com.amazon.firetv.youtube), NOT com.google.android.youtube.tv,
            # which is the Android TV package and is NOT installed.
            # For YouTube we use monkey with the LAUNCHER category instead of
            # `am start -n <pkg>/<activity>` because Amazon's YouTube wrapper
            # doesn't expose a stable launcher activity we can pin to.
            if command == "launch_youtube":
                result = subprocess.run(
                    ["adb", "shell", "monkey", "-p", "com.amazon.firetv.youtube",
                     "-c", "android.intent.category.LAUNCHER", "1"],
                    capture_output=True, text=True, timeout=10
                )
                self.notify_status("Launched Youtube")
                return {
                    "ok": result.returncode == 0,
                    "command": "monkey launch com.amazon.firetv.youtube",
                    "output": result.stdout,
                }

            # App launch commands
            app_commands = {
                "launch_netflix": "com.netflix.ninja/.MainActivity",
                "launch_prime": "com.amazon.venezia/com.amazon.venezia.ui.HomeActivity",
                "launch_disney": "com.disney.disneyplus/.ui.HomeActivity",
                "launch_hulu": "com.hulu.plus.ui.splash.SplashActivity",
                "launch_plex": "com.plexapp.android/.ui.HomeActivity",
                "launch_kodi": "org.xbmc.kodi/.Splash",
            }

            if command in app_commands:
                result = self._adb_shell(["am", "start", "-n", app_commands[command]])
                app_name = command.replace("launch_", "").capitalize()
                self.notify_status(f"Launched {app_name}")
                return result
            
            # Special commands
            if command == "type":
                text = args.get("text", "")
                if not text:
                    return {"ok": False, "error": "No text provided"}
                # Escape spaces for adb
                text = text.replace(" ", "%s")
                return self._adb_shell(["input", "text", text])
            
            if command == "connect":
                return {"ok": self._connect()}
            
            if command == "disconnect":
                return {"ok": self._disconnect()}
            
            return {"ok": False, "error": f"Unknown command: {command}"}
        
        except Exception as e:
            return {"ok": False, "error": str(e)}
    
    def _adb_shell(self, shell_args: list) -> dict:
        """Execute ADB shell command"""
        cmd = ["adb", "shell"] + shell_args
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            return {
                "ok": result.returncode == 0,
                "command": " ".join(cmd),
                "output": result.stdout
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "ADB command timed out"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    
    def handle_voice_command(self, text: str) -> str:
        t = text.lower()
        if "fire tv" in t or "firetv" in t or "firestick" in t:
            if "netflix" in t:
                self.execute("launch_netflix", {})
                return "Opening Netflix on Fire TV"
            elif "youtube" in t:
                self.execute("launch_youtube", {})
                return "Opening YouTube on Fire TV"
            elif "prime" in t or "amazon" in t:
                self.execute("launch_prime", {})
                return "Opening Prime Video on Fire TV"
            elif "disney" in t:
                self.execute("launch_disney", {})
                return "Opening Disney Plus"
            elif "home" in t:
                self.execute("home", {})
                return "Fire TV home screen"
            elif "volume up" in t or "louder" in t:
                self.execute("volume_up", {})
                return "Fire TV volume up"
            elif "volume down" in t or "quieter" in t:
                self.execute("volume_down", {})
                return "Fire TV volume down"
            elif "mute" in t:
                self.execute("mute", {})
                return "Fire TV muted"
            elif "power off" in t or "turn off" in t:
                self.execute("power", {})
                return "Fire TV power toggled"
            elif "back" in t:
                self.execute("back", {})
                return "Fire TV back"
        return None
