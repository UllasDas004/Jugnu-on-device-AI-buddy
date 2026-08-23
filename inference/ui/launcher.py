import webview
import sys
import json
import os
import ctypes
from ctypes import wintypes
import threading

class UIAPI:
    def __init__(self, state):
        self._window = None
        self.state = state

    def get_state(self):
        # sidebar.html calls this via JS to get the hint text!
        return json.dumps(self.state)

    def nudge_action(self, nudge_type, action):
        # When the user clicks "Help/Review" or "Dismiss" in the HTML, this runs.
        # We print it as JSON so the parent process (Jugnu) can read the response.
        print(json.dumps({"event": "nudge_action", "action": action}))
        sys.stdout.flush()
        self.dismiss()

    def feedback(self, f_type):
        print(json.dumps({"event": "feedback", "type": f_type}))
        sys.stdout.flush()
        self.dismiss()

    def confirm_context(self, choice):
        print(json.dumps({"event": "action", "type": "confirm_context", "choice": choice}), flush=True)
    
    def decline(self):
        print(json.dumps({"event": "action", "type": "decline"}), flush=True)
        self.dismiss()
    
    def submit_custom(self, text):
        print(json.dumps({"event": "action", "type": "custom_problem", "text": text}), flush=True)
    
    def get_dashboard_stats(self):
        return json.dumps(self.state.get("dashboard", {}))

    def get_problem_hints(self, slug):
        """Returns JSON array of all hints for a given problem slug, ordered oldest-first."""
        import sqlite3
        try:
            from practice_mode import DB_PATH
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT ph.hint_type, ph.hint_text, ph.code_snapshot,
                       ph.user_feedback, ph.timestamp
                FROM practice_hints ph
                JOIN practice_sessions ps ON ph.session_id = ps.id
                WHERE ps.problem_slug = ?
                ORDER BY ph.timestamp ASC
            """, (slug,)).fetchall()
            conn.close()
            return json.dumps([dict(r) for r in rows])
        except Exception as e:
            print(f"[UIAPI] get_problem_hints error: {e}", flush=True)
            return json.dumps([])

    def set_bug_state(self, state_name):
        """Debug override: forward set_bug_state event to parent via stdout."""
        print(json.dumps({"type": "set_bug_state", "state": state_name}), flush=True)

    def save_geometry(self, x, y, w, h):
        """Persist dashboard window geometry for next open."""
        config_path = os.path.join(os.path.dirname(__file__), 'jugnu_config.json')
        try:
            cfg = {}
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    cfg = json.load(f)
            cfg.update({"dashboard_x": int(x), "dashboard_y": int(y),
                        "dashboard_w": int(w), "dashboard_h": int(h)})
            with open(config_path, 'w') as f:
                json.dump(cfg, f)
        except:
            pass

    def move_window(self, x, y):
        if self._window:
            self._window.move(int(x), int(y))

    def save_position(self, x, y):
        config_path = os.path.join(os.path.dirname(__file__), 'jugnu_config.json')
        try:
            cfg = {}
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    cfg = json.load(f)
            cfg.update({"mascot_x": int(x), "mascot_y": int(y)})
            with open(config_path, 'w') as f:
                json.dump(cfg, f)
        except:
            pass

    def toggle_dashboard(self):
        print(json.dumps({"type": "toggle_dashboard"}), flush=True)

    def dismiss(self):
        if self._window:
            self._window.destroy()

def main():
    if len(sys.argv) < 2:
        print("Usage: python launcher.py <ui_type> [state_file]")
        sys.exit(1)
        
    ui_type = sys.argv[1]
    state_file = sys.argv[2] if len(sys.argv) > 2 else None

    # Load state from the JSON file written by Jugnu
    state = {}
    if state_file and os.path.exists(state_file):
        with open(state_file, 'r', encoding='utf-8') as f:
            state = json.load(f)

    api = UIAPI(state)
    html_file = ""
    width, height = 800, 600

    # --- Get Screen Dimensions ---
    user32 = ctypes.windll.user32
    SW = user32.GetSystemMetrics(0)
    SH = user32.GetSystemMetrics(1)

    x, y = 0, 0
    window_title = "jugnu UI"
    transparent = False

    base_dir = os.path.abspath(os.path.dirname(__file__))
    if ui_type == "jugnu_bug":
        html_file = os.path.join(base_dir, "jugnu_bug.html")
        width, height = 100, 50
        window_title = "jugnuBug"
        # Note: we do NOT use transparent=True here because WebView2's transparent
        # mode is unreliable on many setups and causes a white flash / white background.
        # Instead we use a black background_color (user-accepted alternative).

        # Load saved position if exists, else default bottom-left
        config_path = os.path.join(base_dir, 'jugnu_config.json')
        x, y = 20, SH - 140
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    cfg = json.load(f)
                    x, y = cfg.get("mascot_x", x), cfg.get("mascot_y", y)
            except: pass

    elif ui_type == "nudge_bubble":
        html_file = os.path.join(base_dir, "nudge_bubble.html")
        width, height = 300, 110
        window_title = "jugnuNudgeBubble"
        
        # Nudge bubble goes right next to the mascot's default spot
        x, y = 220, SH - 170
    elif ui_type == "sidebar":
        html_file = os.path.join(base_dir, "sidebar.html")
        width, height = 350, 520
        window_title = "jugnuSidebar"
        x, y = SW - 370, SH - 580

    elif ui_type == "dashboard":
        html_file = os.path.join(base_dir, "dashboard.html")
        width, height = 800, 600
        window_title = "jugnuDashboard"
        x, y = (SW // 2) - (width // 2), 60

    # ── Overlay window setup (jugnu_bug, sidebar, nudge_bubble) ───────────────────
    # Dashboard is a normal interactive app window — no special treatment.
    # All other UI types are overlays that must:
    #   - Stay off the taskbar & Alt+Tab  →  hidden-owner trick
    #   - Never steal focus               →  WS_EX_NOACTIVATE
    _hidden_owner_hwnd = None
    _is_overlay = ui_type in ('jugnu_bug', 'sidebar', 'nudge_bubble')

    if _is_overlay:
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        WS_EX_TOOLWINDOW = 0x00000080
        WS_POPUP         = 0x80000000

        # Register a minimal invisible window class (safe to call multiple times)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = wintypes.LPARAM
        
        WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND,
                                      wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        def _def_wndproc(h, msg, w, l):
            return user32.DefWindowProcW(h, msg, w, l)
        _wndproc_cb = WNDPROC(_def_wndproc)   # must stay alive

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ('style',          ctypes.c_uint),
                ('lpfnWndProc',    WNDPROC),
                ('cbClsExtra',     ctypes.c_int),
                ('cbWndExtra',     ctypes.c_int),
                ('hInstance',      wintypes.HINSTANCE),
                ('hIcon',          wintypes.HANDLE),
                ('hCursor',        wintypes.HANDLE),
                ('hbrBackground',  wintypes.HANDLE),
                ('lpszMenuName',   wintypes.LPCWSTR),
                ('lpszClassName',  wintypes.LPCWSTR),
            ]

        cls_name = 'JugnuHiddenOwner'
        wc = WNDCLASSW()
        wc.lpfnWndProc   = _wndproc_cb
        wc.hInstance     = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = cls_name
        user32.RegisterClassW(ctypes.byref(wc))  # silently no-ops if already registered

        _hidden_owner_hwnd = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW,            # extended style → tool window
            cls_name, '',
            WS_POPUP,                    # style
            0, 0, 0, 0, 0, 0,           # pos/size/parent/menu
            kernel32.GetModuleHandleW(None), None
        )
        print(f"\033[90m[{ui_type}] Hidden owner HWND: {_hidden_owner_hwnd}\033[0m", flush=True)


    # Build window kwargs dynamically
    window_kwargs = {
        'title': window_title,
        'url': html_file,
        'js_api': api,
        'width': width,
        'height': height,
        'x': x,
        'y': y,
        'frameless': True,
        'on_top': True,
    }
    if ui_type == 'jugnu_bug':
        # Black background: avoids WebView2 white flash without relying on
        # the unreliable transparent=True flag.
        window_kwargs['background_color'] = '#000000'
    else:
        window_kwargs['background_color'] = '#1e1e2e'

    window = webview.create_window(**window_kwargs)

    api._window = window

    def _on_loaded(win):
        if ui_type == "nudge_bubble":
            # 3-arg signature: updateNudge(nudgeType, title, body)
            nudge_type = state.get("nudge_type", "reading")
            title = state.get("nudge_title", "Looks like you're stuck")
            msg   = state.get("nudge_msg",   "You've been on this a while — want a <strong>hint</strong> from Jugnu?")
            # Escape single quotes and newlines for JS string safety
            safe_type  = nudge_type.replace("'", "\\'")
            safe_title = title.replace("'", "\\'").replace("\n", " ")[:120]
            safe_body  = msg.replace("'", "\\'").replace("\n", " ")[:280]
            try:
                win.evaluate_js(
                    f"if(window.updateNudge) window.updateNudge('{safe_type}', '{safe_title}', '{safe_body}');"
                )
            except Exception:
                pass

        elif ui_type == "sidebar":
            # Trigger the slide-in entrance animation
            try:
                win.evaluate_js(
                    "var s=document.querySelector('.sidebar');if(s){s.classList.add('animating');}"
                )
            except Exception:
                pass

        elif ui_type in ('jugnu_bug', 'sidebar', 'nudge_bubble'):
            # ── Taskbar / Alt+Tab removal ────────────────────────────────────
            # Critical sequence: HIDE first, then change extended styles and
            # reparent to hidden owner, then SHOW again.  Without the hide/show
            # cycle Windows keeps the old taskbar registration.
            hwnd = ctypes.windll.user32.FindWindowW(None, window_title)
            print(f"\033[90m[{ui_type}] Styling HWND: {hwnd}\033[0m", flush=True)
            if hwnd:
                u32              = ctypes.windll.user32
                GWL_EXSTYLE      = -20
                GWL_HWNDPARENT   = -8
                SW_HIDE          = 0
                SW_SHOWNOACTIVATE = 8   # show without stealing focus
                WS_EX_APPWINDOW  = 0x00040000
                WS_EX_TOOLWINDOW = 0x00000080
                WS_EX_NOACTIVATE = 0x08000000

                # 1. Hide (deregisters from taskbar)
                u32.ShowWindow(hwnd, SW_HIDE)

                # 2. Strip APPWINDOW, add TOOLWINDOW + NOACTIVATE
                cur = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                cur = (cur & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
                u32.SetWindowLongW(hwnd, GWL_EXSTYLE, cur)

                # 3. Reparent to the hidden tool-window owner
                if _hidden_owner_hwnd:
                    u32.SetWindowLongW(hwnd, GWL_HWNDPARENT, _hidden_owner_hwnd)

                # 4. Show again — now invisible to taskbar & Alt+Tab
                u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                print(f"\033[90m[{ui_type}] Taskbar exclusion applied.\033[0m", flush=True)

            # jugnu_bug-specific: start stdin state reader
            if ui_type == "jugnu_bug":
                def _stdin_reader():
                    for line in iter(sys.stdin.readline, ''):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            cmd = json.loads(line)
                            if cmd.get("cmd") == "set_state":
                                state_name = cmd.get("state", "sleeping")
                                print(f"\033[90m[Mascot] Applying state: {state_name}\033[0m", flush=True)
                                try:
                                    win.evaluate_js(f"if(window.setState) window.setState('{state_name}');")
                                except Exception as js_err:
                                    print(f"\033[1;31m[Mascot] evaluate_js failed: {js_err}\033[0m", flush=True)
                        except Exception:
                            pass

                threading.Thread(target=_stdin_reader, daemon=True).start()

    # Removed the invalid 'transparent' argument from start(). It only belongs in create_window().
    # Also explicitly passing 'window' into the on_loaded callback via 'args'
    webview.start(gui='edgechromium', func=_on_loaded, args=(window,), debug=False)

if __name__ == '__main__':
    main()