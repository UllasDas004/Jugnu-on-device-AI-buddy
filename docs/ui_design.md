# 🎨 Jugnu — UI Design & Frontend Architecture

## Decision: C++ WebView2 (Native) as the Final Target

Jugnu's frontend is built with **HTML, CSS, and vanilla JavaScript**, running inside a
**Microsoft WebView2** container hosted by the C++ engine. This makes Jugnu a fully native,
borderless, floating Windows application — the same architecture used by VS Code and
Microsoft Teams — rather than a browser tab or a separate Python subprocess.

**Interim approach (current):** While the C++ WebView2 host is being developed, a Python
`pywebview` bridge renders the notification windows. The HTML/CSS/JS assets are identical
between both approaches, so the frontend work is not throwaway.

---

## Frontend Technology Stack

| Layer | Technology | Reason |
|---|---|---|
| UI container | C++ WebView2 (final) / pywebview (interim) | Native Windows, zero-overhead bridge |
| Structure | HTML5 semantic markup | Clean, maintainable |
| Styling | Vanilla CSS with CSS variables | Zero build-step, full control, easy dark-theme theming |
| Logic | Vanilla JavaScript | Zero dependencies, tiny memory footprint |
| Markdown render | `marked.js` (CDN-less, bundled locally) | Renders Gemma output in chat bubbles |
| Icons | Inline SVG / Unicode emoji | No icon font dependency |

**No React. No Tailwind. No bundler.** Every kilobyte is intentional.

---

## The Native Experience

- **Invisible until needed:** Jugnu runs as a background C++ process with a system tray icon.
- **Proactive notifications:** When the stuck detector fires, Jugnu slides a non-intrusive card into the bottom-right corner of the screen — the user is never required to open a UI manually.
- **On-demand access:** `Ctrl+Space` instantly summons the full floating chat interface.
- **Transparency + rounded corners:** CSS `background: transparent` combined with Win32 layered window styles (`WS_EX_LAYERED`, `SetLayeredWindowAttributes`) gives Jugnu glassmorphism with actual OS-level rounded corners.

---

## Views & Layouts

### A. The Stuck Notification Card (NEW — current implementation focus)
**Purpose:** The 3-stage interaction when Jugnu detects the user is stuck.
Appears as a small card in the bottom-right corner. Never full-screen. Never modal.

**Stage 1 — Soft Nudge (300×130px)**
```
╔══════════════════════════════╗
║ 🔥 Jugnu                    ║
║ Looks like you might be      ║
║ stuck. Need a hand?          ║
║              [No] [Yes →]   ║
╚══════════════════════════════╝
Auto-dismisses after 30s (= "No")
```

**Stage 2 — Context Confirmation (380×200px)**
```
╔══════════════════════════════════╗
║ Here's what I see:               ║
║ 💻 Last IDE: Antigravity (2 min) ║
║ 📄 Last file: ai_engine.py       ║
║ 📋 Clipboard: "ollama.chat(..."  ║
║                                  ║
║ Is this the problem area?        ║
║   [Cancel] [Describe it] [Yes!] ║
╚══════════════════════════════════╝
```

**Stage 3 — AI Insight (460×280px, scrollable)**
```
╔══════════════════════════════════╗
║ 💡 Jugnu says:                   ║
║                                  ║
║ Check that your Ollama server    ║
║ is running before calling        ║
║ ollama.chat(). The model...      ║
║                                  ║
║                  [Got it! ✓]    ║
╚══════════════════════════════════╝
```

---

### B. The Floating Chat (Full Interface — Phase 2)
**Purpose:** On-demand chat triggered by `Ctrl+Space`.
- Centered on screen, 600×500px
- Chat history that expands upward
- "Current Context" status bar at bottom (e.g., *In: ai_engine.py | Antigravity IDE*)
- Glassmorphism dark background with subtle border glow

### C. The Memory Dashboard (Phase 3)
- App priority leaderboard (EMA scores)
- Recent episodic memory timeline with importance scores
- Core persona facts the AI has extracted

### D. Settings Panel (Phase 3)
- Pause monitoring toggle
- Model selection (switch between local models)
- Clear database danger zone

---

## IPC: The WebView2 Bridge

The UI communicates with C++ through a **zero-latency direct JSON bridge** — no HTTP, no sockets.

### Sending data from UI (JS) → C++:
```javascript
// User types a message in the chat UI
function sendMessage(text) {
    window.chrome.webview.postMessage(JSON.stringify({
        action: "chat",
        message: text
    }));
}

// User clicks "Yes" in the nudge card
function onUserConfirmedStuck() {
    window.chrome.webview.postMessage(JSON.stringify({
        action: "stuck_confirmed"
    }));
}
```

### Receiving data from C++ → UI (JS):
```javascript
window.chrome.webview.addEventListener('message', event => {
    const data = JSON.parse(event.data);

    if (data.type === 'token') {
        // Stream LLM tokens into chat bubble in real-time
        appendTokenToChatBubble(data.text);
    } else if (data.type === 'status_update') {
        updateContextBar(data.currentApp, data.lastFile);
    } else if (data.type === 'show_nudge') {
        showStuckCard(data.contextSummary);
    }
});
```

### Interim Python Bridge (pywebview):
During Python-side development, `pywebview` exposes Python functions directly to JavaScript via `js_api`, replacing the C++ bridge transparently:
```javascript
// Same button click, different backend — API surface is identical
document.getElementById('btn-yes').onclick = () => pywebview.api.on_yes();
```
This means the HTML/CSS/JS frontend is written once and works with both backends.

---

## Visual Design System

```css
:root {
  /* Jugnu Color Palette — Catppuccin Mocha inspired */
  --bg-base:    #1e1e2e;   /* main background */
  --bg-surface: #313244;   /* card/input surface */
  --border:     #45475a;   /* subtle borders */
  --text-main:  #cdd6f4;   /* primary text */
  --text-sub:   #a6adc8;   /* secondary text */
  --accent-blue:#89b4fa;   /* primary buttons */
  --accent-green:#a6e3a1;  /* success / insight */
  --accent-red: #f38ba8;   /* danger / errors */

  /* Typography */
  --font: 'Segoe UI', system-ui, sans-serif;

  /* Geometry */
  --radius: 12px;
  --shadow: 0 8px 32px rgba(0,0,0,0.4);
}
```

All notification cards use: `backdrop-filter: blur(12px)` for the glassmorphism effect,
`border-radius: var(--radius)` for rounded corners, and a subtle `1px solid var(--border)` outline.

---

## Animation: Slide-In on Appear
```css
@keyframes slideInUp {
  from { opacity: 0; transform: translateY(20px); }
  to   { opacity: 1; transform: translateY(0); }
}

.jugnu-card {
  animation: slideInUp 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
}
```

---

## Development Priority Order

1. ✅ **Stuck detection logic** (Python `state_manager.py`, `ipc_client.py`)
2. ✅ **Notification card HTML/CSS** — all 3 stages implemented
3. ✅ **pywebview bridge** — render cards from Python with thread-safety (interim)
4. 🔲 **C++ WebView2 host** — embed Chromium in the C++ process (final)
5. 🔲 **Full chat UI** — on-demand `Ctrl+Space` interface
6. 🔲 **Memory Dashboard + Settings** — Phase 3
