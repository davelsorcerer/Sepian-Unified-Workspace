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
                    print("[DEBUG] _dev_approval_request show result:", result_box["value"])
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
        print("[DEBUG] _dev_approval_request returning:", result_box["value"])
        return result_box["value"] or {"ok": False, "decision": "deny"}
