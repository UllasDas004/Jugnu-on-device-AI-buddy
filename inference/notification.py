import webview
import threading
import time

# ── Global state ──────────────────────────────────────────────────────

is_generating = False
_last_trigger_time = 0.0
_COOLDOWN_YES = 20 * 60     # 20 min after successful insight
_COOLDOWN_NO = 15 * 60      # 15 min after dismissal


def in_cooldown():
    return (time.time() - _last_trigger_time) < _COOLDOWN_YES

def _start_cooldown(seconds):
    global _last_trigger_time, _COOLDOWN_YES
    _last_trigger_time = time.time()
    _COOLDOWN_YES = seconds

# ── Stage 1: Soft Nudge ───────────────────────────────────────────────

NUDGE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: #1e1b1a; color: #e8d5c4; height: 130px;
            display: flex; flex-direction: column; justify-content: center;
            padding: 16px 20px; border: 1px solid #3d3530;
            border-radius: 12px; overflow: hidden;
        }
        .title { font-size: 13px; font-weight: 700; color: #f0a875; margin-bottom: 6px; }
        .msg { font-size: 12px; color: #c4b09a; line-height: 1.5; margin-bottom: 12px; }
        .btns { display: flex; gap: 8px; justify-content: flex-end; }
        button {
            border: none; border-radius: 8px; padding: 6px 14px; font-size: 12px;
            cursor: pointer; font-family: inherit; transition: opacity 0.15s;
        }
        button:hover { opacity: 0.8; }
        .no { background: #2e2926; color: #a89585; }
        .yes { background: #f0a875; color: #1e1b1a; font-weight: 600; }
    </style>
</head>
<body>
    <div class="title">🔥 Jugnu</div>
    <div class="msg">Looks like you might be stuck.<br>Need a hand?</div>
    <div class="btns">
        <button class="no"  onclick="pywebview.api.no()">No thanks</button>
        <button class="yes" onclick="pywebview.api.yes()">Yes, help me!</button>
    </div>
    <script>setTimeout(() => pywebview.api.no(), 30000);</script>
</body>
</html>
"""

def show_nudge():
    result = {"value": False}
    done = threading.Event()

    class Api:
        def yes(self):
            result['value'] = True
            win.destroy()
        def no(self):
            win.destroy()
        
    api = Api()
    win = webview.create_window(
        "Jugnu", html = NUDGE_HTML, js_api = api,
        width = 360, height = 160, on_top = True,
        frameless = True, transparent = True
    )
    win.events.closed += lambda: done.set()

    done.wait()
    return result["value"]

# ── Stage 2: Context Confirmation ────────────────────────────────────
def build_context_html(summary):
    # Escape for HTML embedding
    safe = summary.replace("\n", "<br>").replace('"', '&quot;')
    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: #1e1b1a; color: #e8d5c4; padding: 16px 20px;
            border: 1px solid #3d3530; border-radius: 12px; overflow: hidden;
        }}
        .label {{
            font-size: 11px; font-weight: 700; color: #a89585;
            text-transform: uppercase; letter-spacing: .05em; margin-bottom: 8px;
        }}
        .summary {{
            font-size: 12px; color: #c4b09a; line-height: 1.7; margin-bottom: 14px;
            background: #2a2523; padding: 10px 12px; border-radius: 8px;
        }}
        .q {{ font-size: 12px; color: #e8d5c4; margin-bottom: 12px; }}
        .btns {{ display: flex; gap: 8px; justify-content: flex-end; }}
        button {{
            border: none; border-radius: 8px; padding: 6px 12px; font-size: 11px;
            cursor: pointer; font-family: inherit; transition: opacity 0.15s;
        }}
        button:hover {{ opacity: 0.8; }}
        .cancel {{ background: #2e2926; color: #a89585; }}
        .describe {{ background: #2e2926; color: #c4b09a; }}
        .yes {{ background: #f0a875; color: #1e1b1a; font-weight: 600; }}
    </style>
</head>
<body>
    <div class="label">Here's what I see</div>
    <div class="summary">{safe}</div>
    <div class="q">Is this the problem area?</div>
    <div class="btns">
        <button class="cancel"   onclick="pywebview.api.cancel()">Cancel</button>
        <button class="describe" onclick="pywebview.api.describe()">No, I'll describe it</button>
        <button class="yes"      onclick="pywebview.api.confirm()">Yes, help!</button>
    </div>
</body>
</html>
"""

def show_context_dialog(summary):
    result = {"value": "cancel"}
    done = threading.Event()

    class Api:
        def confirm(self):
            result["value"] = "yes"
            win.destroy()
        def describe(self):
            result["value"] = "custom"
            win.destroy()
        def cancel(self):
            win.destroy()

    api = Api()
    win = webview.create_window(
        "Jugnu — Context", html=build_context_html(summary),
        js_api=api, width=400, height=240,
        on_top=True, frameless=True, transparent=True
    )
    win.events.closed += lambda: done.set()
    done.wait()
    return result["value"]

# ── Stage 2b: Custom Text Input ───────────────────────────────────────

INPUT_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: #1e1b1a; color: #e8d5c4; padding: 16px 20px;
            border: 1px solid #3d3530; border-radius: 12px; overflow: hidden;
        }
        .label { font-size: 13px; font-weight: 600; color: #e8d5c4; margin-bottom: 10px; }
        textarea {
            width: 100%; height: 80px; background: #2a2523; color: #e8d5c4;
            border: 1px solid #3d3530; border-radius: 8px; padding: 10px;
            font-family: inherit; font-size: 12px; resize: none; outline: none;
        }
        .hint { font-size: 10px; color: #7a6a60; margin: 6px 0 10px; }
        .btns { display: flex; gap: 8px; justify-content: flex-end; }
        button {
            border: none; border-radius: 8px; padding: 6px 14px; font-size: 12px;
            cursor: pointer; font-family: inherit; transition: opacity 0.15s;
        }
        button:hover { opacity: 0.8; }
        .cancel { background: #2e2926; color: #a89585; }
        .go { background: #a6e3a1; color: #1e1b1a; font-weight: 600; }
    </style>
</head>
<body>
    <div class="label">What are you stuck on?</div>
    <textarea id="txt" placeholder="Describe the problem..."></textarea>
    <div class="hint">Ctrl + Enter to submit</div>
    <div class="btns">
        <button class="cancel" onclick="pywebview.api.cancel()">Cancel</button>
        <button class="go" onclick="submit()">Get Help →</button>
    </div>
    <script>
        function submit() {
            const t = document.getElementById('txt').value.trim();
            if(t) pywebview.api.submit(t);
        }
        document.addEventListener('keydown', e => {
            if(e.ctrlKey && e.key === 'Enter') submit();
        });
    </script>
</body>
</html>
"""

def show_text_input():
    result = {"value": None}
    done = threading.Event()

    class Api:
        def submit(self, text):
            result["value"] = text
            win.destroy()
        def cancel(self):
            win.destroy()
    
    api = Api()
    win = webview.create_window(
        "Jugnu — Tell me more", html=INPUT_HTML, js_api=api,
        width=400, height=220, on_top=True, frameless=True, transparent=True
    )
    win.events.closed += lambda: done.set()
    done.wait()
    return result["value"]

# ── Stage 3: Insight Window ───────────────────────────────────────────
def build_insight_html(text):
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: #1e1b1a; color: #e8d5c4; padding: 16px 20px;
            border: 1px solid #3d3530; border-radius: 12px;
            display: flex; flex-direction: column; height: 100%; overflow: hidden;
        }}
        .title {{ font-size: 13px; font-weight: 700; color: #a6e3a1; margin-bottom: 10px; }}
        .content {{
            flex: 1; overflow-y: auto; font-size: 12px; color: #c4b09a; line-height: 1.7;
            background: #2a2523; padding: 12px; border-radius: 8px; margin-bottom: 12px;
        }}
        button {{
            border: none; border-radius: 8px; padding: 7px 18px; font-size: 12px;
            cursor: pointer; font-family: inherit; background: #89b4fa;
            color: #1e1b1a; font-weight: 600; align-self: flex-end; transition: opacity 0.15s;
        }}
        button:hover {{ opacity: 0.8; }}
    </style>
</head>
<body>
    <div class="title">💡 Jugnu says:</div>
    <div class="content">{safe}</div>
    <button onclick="pywebview.api.close()">Got it, thanks! ✓</button>
</body>
</html>
"""

def show_insight(text):
    done = threading.Event()

    class Api:
        def close(self):
            win.destroy()

    api = Api()
    win = webview.create_window(
        "Jugnu — Insight", html=build_insight_html(text),
        js_api=api, width=460, height=310,
        on_top=True, frameless=True, transparent=True
    )
    win.events.closed += lambda: done.set()
    done.wait()

# ── Main Orchestrator ─────────────────────────────────────────────────
def trigger_flow(state, engine):
    global is_generating

    if is_generating or in_cooldown():
        return

    # Stage 1
    if not show_nudge():
        _start_cooldown(_COOLDOWN_NO)
        print("\033[90m[Notification] User declined. 15-min cooldown.\033[0m", flush=True)
        return
    
    # Stage 2
    summary = state.get_context_summary()
    choice = show_context_dialog(summary)

    if choice == "cancel":
        _start_cooldown(_COOLDOWN_NO)
        print("\033[90m[Notification] User cancelled. 15-min cooldown.\033[0m", flush=True)
        return

    custom_problem = None
    if choice == "custom":
        custom_problem = show_text_input()
        if not custom_problem:
            _start_cooldown(_COOLDOWN_NO)
            print("\033[90m[Notification] User cancelled input. 15-min cooldown.\033[0m", flush=True)
            return

    # Stage 3: Query AI
    is_generating = True
    print("\n\033[1;36m[Notification] Querying AI...\033[0m", flush=True)
    try:
        context = state.generate_prompt_context(custom_problem = custom_problem)
        insight = engine.generate_insight(context)
        print("\033[1;32m[Jugnu AI Buddy] Insight generated successfully.\033[0m\n", flush=True)

    except Exception as e:
        insight = f"Sorry, I couldn't generate an insight right now.\nError: {e}"
    
    finally:
        is_generating = False

    _start_cooldown(_COOLDOWN_YES)
    show_insight(insight)

