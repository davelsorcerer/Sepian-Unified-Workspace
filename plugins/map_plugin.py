# plugins/map_plugin.py
import os
import json
from PIL import Image, ImageDraw, ImageTk
import tkinter as tk

class MapPlugin:
    name = "property_map"
    version = "0.1"
    
    def get_description(self):
        return "Shows interactive zones on property map via voice"

    def get_commands(self):
        return ["show", "where is", "hide map"]  # Voice triggers

    def execute(self, command, args):
        # Handle phrases like: "show roses", "where is herbs", "hide map"
        if "hide" in command.lower():
            self.hide_map()
            return {"ok": True, "msg": "Map hidden"}
        
        # Extract zone name (e.g., "roses" from "show roses")
        zone = None
        for word in ["show", "where is"]:
            if word in command.lower():
                zone = command.lower().replace(word, "").strip()
                break
        
        if not zone or zone not in self.zones:
            return {"ok": False, "msg": f"Zone '{zone}' not found. Try: {', '.join(self.zones.keys())}"}
        
        self.show_zone(zone)
        return {"ok": True, "msg": f"Showing {zone}"}

    def setup(self, sepian_app):
        """Called once when Sepian loads — does all the heavy lifting"""
        self.sepian = sepian_app
        self.map_path = os.path.join(os.path.dirname(__file__), "property_map.jpg")
        self.zones = self.load_zones()  # Reads zones from zones.json (see below)
        self.photo_img = None  # For Tkinter image reference
        self.map_window = None  # Tracks if map is open

    def load_zones(self):
        """Reads zone names + coordinates from zones.json (Adam creates this once)"""
        zones_path = os.path.join(os.path.dirname(__file__), "zones.json")
        if not os.path.exists(zones_path):
            # Default zones if file missing — Adam replaces this!
            return {
                "roses": [100, 200, 300, 400],  # [x1, y1, x2, y2]
                "herbs": [350, 150, 500, 300],
                "veggies": [50, 400, 250, 600]
            }
        with open(zones_path, "r") as f:
            return json.load(f)

    def show_zone(self, zone_name):
        """Draws highlight on map and shows it in a window"""
        if self.map_window and self.map_window.winfo_exists():
            self.map_window.destroy()  # Close old if open

        # Load base image
        base = Image.open(self.map_path).convert("RGBA")
        overlay = Image.new("RGBA", base.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)

        # Get zone coords (x1, y1, x2, y2)
        x1, y1, x2, y2 = self.zones[zone_name]
        # Draw semi-transparent green rectangle
        draw.rectangle([x1, y1, x2, y2], fill=(0, 255, 0, 80), outline=(0, 200, 0, 200), width=3)

        # Combine and show
        combined = Image.alpha_composite(base, overlay)
        self.photo_img = ImageTk.PhotoImage(combined)  # Keep reference!

        # Create popup window
        self.map_window = tk.Toplevel(self.sepian.master)
        self.map_window.title(f"{zone_name.title()} Zone")
        self.map_window.geometry(f"{base.width}x{base.height}+100+100")  # Position near top-left
        self.map_window.attributes("-topmost", True)  # Stay on Sepian window
        
        label = tk.Label(self.map_window, image=self.photo_img)
        label.pack()
        
        # Make clicking the zone trigger TTS description
        label.bind("<Button-1>", lambda e: self.describe_zone(zone_name))
        
        # Auto-close after 10s (optional)
        self.map_window.after(10000, lambda: self.map_window.destroy() if self.map_window.winfo_exists() else None)

    def describe_zone(self, zone_name):
        """Speaks a description when zone is clicked"""
        descriptions = {
            "roses": "This is the rose garden — David Austin varieties, peak bloom in June.",
            "herbs": "Herb spiral: basil, thyme, rosemary, and mint near the stepping stones.",
            "veggies": "Raised beds for tomatoes, peppers, and lettuce — composted every spring."
        }
        desc = descriptions.get(zone_name, "A special part of the garden.")
        self.sepian.tts.speak(desc, blocking=False)

    def hide_map(self):
        """Closes the map window if open"""
        if self.map_window and self.map_window.winfo_exists():
            self.map_window.destroy()
            self.map_window = None

    def shutdown(self):
        self.hide_map()

def get_plugin_class():
    return MapPlugin
