from sepian_plugin import SepianPlugin
import os
import tkinter as tk
from tkinter import ttk

# PIL is initialised in sepianai.py at startup. Import the flag from
# there so we can refuse cleanly when it's not available, instead of
# crashing on `PIL_AVAILABLE` not being defined. Also import the
# Image / ImageTk names so we can resize and create PhotoImage for
# the fullscreen canvas.
try:
    from sepianai import PIL_AVAILABLE
except ImportError:
    PIL_AVAILABLE = False

if PIL_AVAILABLE:
    try:
        from PIL import Image, ImageTk
    except ImportError:
        PIL_AVAILABLE = False


class FullscreenHeadPlugin(SepianPlugin):
    """Toggles ONLY the talking head canvas into fullscreen.

    Strategy: when entering fullscreen, hide the right_panel (which
    contains chat / input / toolbar) and let left_panel (the head side
    of the PanedWindow) expand to fill the window. On exit, restore
    the PanedWindow so right_panel comes back.

    Press Esc, click the head button again, or call the command to
    exit.
    """

    def __init__(self):
        self.name = "Fullscreen Head"
        self.enabled = True
        self.app = None
        self._is_fullscreen = False
        self._toggle_button = None
        # Saved layout state for restore-on-exit.
        self._saved = {}

    def get_description(self):
        return ("Makes the talking head canvas fullscreen while keeping "
                "chat, input, and toolbar visible as compact overlays.")

    def set_app(self, app):
        self.app = app
        self._add_ui_controls()

    def get_commands(self):
        return ["toggle_head_fullscreen"]

    def execute(self, command, args):
        if not self.app:
            return {"ok": False, "error": "Plugin not initialized"}
        if command == "toggle_head_fullscreen":
            return self._toggle()
        return {"ok": False, "error": f"Unknown command: {command}"}

    # ------------------------------------------------------------------
    # UI control
    # ------------------------------------------------------------------
    def _add_ui_controls(self):
        if not self.app:
            return
        master = self.app.master
        # Always create a fresh small toolbar at the very top of master,
        # above everything else. This makes the button discoverable
        # regardless of UI layout differences.
        toolbar = ttk.Frame(master)
        toolbar.pack(side="top", fill="x", padx=10, pady=(10, 0))
        self._toggle_button = ttk.Button(
            toolbar,
            text="Head Fullscreen: Off",
            command=self._toggle,
            width=22,
        )
        self._toggle_button.pack(side="left", padx=(0, 6))
        self._toolbar = toolbar

    def _set_button(self):
        if not self._toggle_button:
            return
        self._set_button_text()

    def _set_button_text(self):
        try:
            self._toggle_button.config(
                text="Head Fullscreen: On" if self._is_fullscreen
                     else "Head Fullscreen: Off"
            )
        except Exception:
            pass

    def _toggle(self):
        try:
            if self._is_fullscreen:
                r = self._exit()
            else:
                r = self._enter()
            self._set_button_text()
            return r
        except Exception as e:
            import traceback
            print(f"[FullscreenHead] toggle exception: {e}", flush=True)
            traceback.print_exc()
            return {"ok": False, "error": f"toggle failed: {e}"}

    # ------------------------------------------------------------------
    # Locate UI pieces by walking the widget tree.
    # ------------------------------------------------------------------
    def _walk(self, widget):
        """Yield widget and all its descendants."""
        yield widget
        for child in widget.winfo_children():
            yield from self._walk(child)

    def _find_by_class(self, root, class_name):
        for w in self._walk(root):
            try:
                if w.winfo_class() == class_name:
                    return w
            except Exception:
                continue
        return None

    def _head_canvas(self):
        if not self.app:
            return None
        return getattr(self.app, "head_canvas", None)

    def _left_panel(self):
        """The Tkinter.Frame inside the PanedWindow that holds the head."""
        canvas = self._head_canvas()
        if canvas is None:
            return None
        return canvas.master  # left_panel

    def _right_panel(self):
        """The Tkinter.Frame inside the PanedWindow that holds chat/input/toolbar."""
        lp = self._left_panel()
        if lp is None:
            return None
        main_pane = lp.master
        if not isinstance(main_pane, tk.PanedWindow):
            return None
        for sibling in main_pane.winfo_children():
            if sibling is not lp:
                return sibling
        return None

    # ------------------------------------------------------------------
    # Enter / Exit
    # ------------------------------------------------------------------
    def _enter(self):
        """Open a fullscreen Toplevel showing the head at large size.

        Strategy:
          1. Hide the original head canvas (it stays in the widget
             tree but isn't packed/visible).
          2. Forget right_panel from the PanedWindow so the chat side
             disappears.
          3. Open a fullscreen Toplevel with a NEW canvas.
          4. Resize every head image to the screen size using PIL
             and create new PhotoImage objects (PhotoImage can't
             scale). The Toplevel canvas uses these scaled images.
          5. Wrap update_animation so it draws the CURRENT viseme
             into the fullscreen canvas at the scaled size.
        """
        app = self.app
        master = app.master
        head = getattr(app, "head", None)
        if head is None or not getattr(head, "images", None):
            return {"ok": False,
                    "error": "head animation not available on app"}
        if not PIL_AVAILABLE:
            return {"ok": False,
                    "error": "PIL not available — cannot scale head images"}

        try:
            screen_w = master.winfo_screenwidth()
            screen_h = master.winfo_screenheight()

            # 1. Find panels.
            left_panel = self._left_panel()
            right_panel = self._right_panel()
            main_pane = left_panel.master if left_panel else None

            self._saved = {
                "right_panel": right_panel,
                "main_pane": main_pane,
            }

            # 2. Forget right_panel from the PanedWindow so the chat
            #    side disappears in the original window. We DO NOT hide
            #    the head canvas or status label — they stay visible in
            #    the original window alongside the fullscreen Toplevel.
            if right_panel is not None and isinstance(main_pane, tk.PanedWindow):
                try:
                    main_pane.forget(right_panel)
                except Exception:
                    pass

            # 3. Hide this plugin's toolbar (the one with the toggle
            #    button) — it's no longer useful while fullscreen.
            if hasattr(self, "_toolbar") and self._toolbar is not None:
                try:
                    self._toolbar.pack_forget()
                except Exception:
                    pass

            # 4. Build SCALED versions of every head image, preserving
            #    the source aspect ratio. The source PNGs are portrait
            #    (~400x450) and the screen is landscape, so we fit-to-
            #    height (scale by screen_h / src_h) and centre the
            #    image horizontally. This avoids the stretched look
            #    you get from a naive resize to (screen_w, screen_h).
            src_w, src_h = 400, 450  # known head PNG dimensions
            scale = screen_h / src_h
            new_w = int(src_w * scale)
            new_h = screen_h
            x_offset = (screen_w - new_w) // 2
            scaled = {}
            for name, photo in head.images.items():
                try:
                    fname_map = {"base": "default.png",
                                 "ai": "AI.png",
                                 "blink": "Eyes-closed.png"}
                    fname = fname_map.get(name, f"{name}.png")
                    pil_im = Image.open(fname).convert("RGBA")
                    pil_im = pil_im.resize((new_w, new_h),
                                           Image.LANCZOS)
                    scaled[name] = ImageTk.PhotoImage(pil_im)
                except Exception as e:
                    print(f"[FullscreenHead] skip image {name}: {e}",
                          flush=True)
                    continue
            scaled_gaze = {}
            for name, photo in getattr(head, "gaze_images", {}).items():
                try:
                    gaze_files = {
                        "left": "lookingleft.png",
                        "center": "middlelook.png",
                        "right": "lookingright.png",
                    }
                    gaze_path = os.path.join(
                        os.path.dirname(__file__), "..",
                        gaze_files[name],
                    )
                    if name == "right" and not os.path.exists(gaze_path):
                        gaze_path = os.path.join(
                            os.path.dirname(__file__), "..",
                            "lookingleft.png",
                        )
                        pil_im = Image.open(gaze_path).convert("RGBA")
                        pil_im = pil_im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                    else:
                        pil_im = Image.open(gaze_path).convert("RGBA")
                    pil_im = pil_im.resize((new_w, new_h), Image.LANCZOS)
                    scaled_gaze[name] = ImageTk.PhotoImage(pil_im)
                except Exception as e:
                    print(f"[FullscreenHead] skip gaze image {name}: {e}",
                          flush=True)
            if not scaled:
                return {"ok": False, "error": "failed to scale head images"}
            self._fs_images = scaled
            self._fs_gaze_images = scaled_gaze
            self._fs_x_offset = x_offset
            self._fs_y_offset = 0

            # 6. Create the fullscreen Toplevel.
            top = tk.Toplevel(master)
            top.title("Head Fullscreen")
            top.configure(bg="#000000")
            top.bind("<Escape>", lambda e: self._exit())
            top.protocol("WM_DELETE_WINDOW", self._exit)

            try:
                top.attributes("-fullscreen", True)
                top.attributes("-topmost", True)
            except Exception:
                try:
                    top.state("zoomed")
                except Exception:
                    pass

            fs_canvas = tk.Canvas(top, bg="#000000",
                                  width=screen_w, height=screen_h,
                                  highlightthickness=0)
            fs_canvas.pack(fill="both", expand=True)

            # Place the base image first so the canvas isn't blank.
            base = scaled.get("base")
            if base is not None:
                fs_canvas.create_image(x_offset, 0, image=base, anchor="nw")

            # Status label at the top — mirrors the app's status_label
            # (which is hidden because right_panel is gone). The label
            # gets updated whenever the main app calls _set_status.
            self._fs_status_var = tk.StringVar(value="Status: Ready")
            try:
                status_label = tk.Label(
                    top, textvariable=self._fs_status_var,
                    fg=getattr(app, "user_col", "#ff3333"),
                    bg="#000000",
                    font=("Segoe UI", 14, "bold"),
                )
                status_label.place(relx=0.5, rely=0.0, x=0, y=20,
                                   anchor="n")
            except Exception:
                pass

            # Hook into the app's _set_status so the fullscreen status
            # label updates in real time.
            self._install_status_hook(app, self._fs_status_var)

            # Hint at the bottom.
            try:
                hint = tk.Label(top, text="Press Esc to exit fullscreen",
                                fg="#888888", bg="#000000",
                                font=("Segoe UI", 10))
                hint.place(relx=0.5, rely=1.0, x=0, y=-20, anchor="s")
            except Exception:
                pass

            self._fs_window = top
            self._fs_canvas = fs_canvas

            # 7. Install animation hook.
            self._install_fs_animation_hook(head, fs_canvas, scaled, scaled_gaze)

            self._is_fullscreen = True
            print(f"[FullscreenHead] Entered fullscreen "
                  f"({screen_w}x{screen_h}).", flush=True)
            return {"ok": True, "message": "Head fullscreen ON (Esc to exit)"}
        except Exception as e:
            import traceback
            print(f"[FullscreenHead] _enter exception: {e}", flush=True)
            traceback.print_exc()
            return {"ok": False, "error": f"enter failed: {e}"}

    def _install_fs_animation_hook(self, head, fs_canvas, scaled_images,
                                   scaled_gaze_images):
        """Wrap head.update_animation so the fullscreen canvas shows
        the current viseme at fullscreen size (using the pre-scaled
        PhotoImage objects in `scaled_images`)."""
        if not hasattr(head, "_orig_update_animation"):
            head._orig_update_animation = head.update_animation

        overlay_id = [None]
        gaze_id = [None]
        blink_id = [None]

        def _wrapped_update_animation():
            # Call the original first so the (now-hidden) small canvas
            # still updates — keeps state consistent if we exit.
            try:
                head._orig_update_animation()
            except Exception:
                pass
            # Mirror the current viseme to the fullscreen canvas at
            # the SCALED size.
            try:
                viseme_name = head._get_current_viseme()
                current = scaled_images.get(viseme_name)
                if current is None:
                    current = scaled_images.get("base")
                gaze = getattr(head, "gaze", "center")
                gaze_image = scaled_gaze_images.get(gaze)
                if gaze_image is not None:
                    if gaze_id[0] is None:
                        gaze_id[0] = fs_canvas.create_image(
                            getattr(self, "_fs_x_offset", 0),
                            getattr(self, "_fs_y_offset", 0),
                            image=gaze_image, anchor="nw")
                    else:
                        fs_canvas.itemconfig(gaze_id[0], image=gaze_image)
                elif gaze_id[0] is not None:
                    fs_canvas.itemconfig(gaze_id[0], image="")
                blink_image = scaled_images.get("blink")
                if blink_image is not None and getattr(head, "_blink_active", False):
                    if blink_id[0] is None:
                        blink_id[0] = fs_canvas.create_image(
                            getattr(self, "_fs_x_offset", 0),
                            getattr(self, "_fs_y_offset", 0),
                            image=blink_image, anchor="nw")
                    else:
                        fs_canvas.itemconfig(blink_id[0], image=blink_image)
                elif blink_id[0] is not None:
                    fs_canvas.itemconfig(blink_id[0], image="")
                if current is not None:
                    if overlay_id[0] is None:
                        overlay_id[0] = fs_canvas.create_image(
                            getattr(self, "_fs_x_offset", 0),
                            getattr(self, "_fs_y_offset", 0),
                            image=current, anchor="nw")
                    else:
                        fs_canvas.itemconfig(overlay_id[0], image=current)
            except Exception:
                pass

        head.update_animation = _wrapped_update_animation

    def _install_status_hook(self, app, fs_status_var):
        """Wrap app._set_status so the fullscreen status label updates
        whenever the main app's status changes."""
        if not hasattr(app, "_orig_set_status"):
            app._orig_set_status = app._set_status

        def _wrapped_set_status(text):
            # Call the original first (updates the original label, if any).
            try:
                app._orig_set_status(text)
            except Exception:
                pass
            # Mirror into the fullscreen label.
            try:
                fs_status_var.set(f"Status: {text}")
            except Exception:
                pass

        app._set_status = _wrapped_set_status

    def _exit(self):
        if not self._saved and not getattr(self, "_fs_window", None):
            self._is_fullscreen = False
            return {"ok": True, "message": "Head fullscreen OFF (nothing to restore)"}
        try:
            app = self.app
            master = app.master
            head = getattr(app, "head", None)

            # 1. Restore the original update_animation method on the
            #    head (we wrapped it during _enter).
            if head is not None and hasattr(head, "_orig_update_animation"):
                try:
                    head.update_animation = head._orig_update_animation
                    del head._orig_update_animation
                except Exception:
                    pass

            # 1b. Restore the original _set_status method on the app.
            if hasattr(app, "_orig_set_status"):
                try:
                    app._set_status = app._orig_set_status
                    del app._orig_set_status
                except Exception:
                    pass

            # 2. Destroy the fullscreen Toplevel.
            fs = getattr(self, "_fs_window", None)
            if fs is not None:
                try:
                    fs.destroy()
                except Exception:
                    pass
                self._fs_window = None
                self._fs_canvas = None

            # Drop the scaled image refs so they can be GC'd.
            self._fs_images = None
            self._fs_gaze_images = None

            # 3. Unbind Escape on master (we re-bound it).
            try:
                master.unbind("<Escape>")
            except Exception:
                pass

            # 4. Restore the original layout: re-add right_panel to
            #    the PanedWindow.
            saved = self._saved or {}
            right_panel = saved.get("right_panel")
            main_pane = saved.get("main_pane")
            if right_panel is not None and isinstance(main_pane, tk.PanedWindow):
                try:
                    main_pane.add(right_panel, minsize=500, sticky="nsew")
                except Exception:
                    pass

            # 5. Restore the head canvas size (it was unchanged in
            #    _enter, but reset just in case).
            canvas = self._head_canvas()
            if canvas is not None:
                try:
                    canvas.configure(
                        width=400,
                        height=450,
                        bg=getattr(app, "bg", "#000000"),
                        highlightthickness=0,
                    )
                except Exception:
                    pass

            # 6. Restore our toolbar.
            if hasattr(self, "_toolbar") and self._toolbar is not None:
                try:
                    self._toolbar.pack(in_=master, side="top",
                                        fill="x", padx=10, pady=(10, 0))
                except Exception:
                    pass

            # 7. Force redraw.
            if head is not None and hasattr(head, "update_animation"):
                try:
                    head.update_animation()
                except Exception:
                    pass

            self._is_fullscreen = False
            self._saved = {}
            print("[FullscreenHead] Exited fullscreen.", flush=True)
            return {"ok": True, "message": "Head fullscreen OFF"}
        except Exception as e:
            import traceback
            print(f"[FullscreenHead] _exit exception: {e}", flush=True)
            traceback.print_exc()
            self._is_fullscreen = False
            self._saved = {}
            return {"ok": False, "error": f"exit failed: {e}"}

    def _right_panel_from_place(self):
        """Find the right_panel widget even if the PanedWindow layout
        has been destroyed (right_panel may be placed in master now)."""
        if not self.app:
            return None
        master = self.app.master
        # Look for a Frame child that contains a ScrolledText (chat).
        for w in master.winfo_children():
            if isinstance(w, tk.Frame):
                for child in self._walk(w):
                    try:
                        if child.winfo_class() == "ScrolledText":
                            return w
                    except Exception:
                        continue
        return None


def get_plugin():
    return FullscreenHeadPlugin()
