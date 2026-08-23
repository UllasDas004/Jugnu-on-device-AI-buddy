# 🎨 Jugnu — UI Design & Frontend Architecture

## Overview

Jugnu's UI is a **multi-window overlay system** composed of four distinct floating windows, all spawned on-demand as child `subprocess.Popen` processes from the Python inference engine. Each window is rendered by `launcher.py` using **`pywebview`** backed by Microsoft's native WebView2 engine.

The frontend is written in **plain HTML, CSS, and vanilla JavaScript** — no React, no Tailwind, no bundler. Every kilobyte is intentional.

---

## Final Architecture: C++ WebView2 Target vs. Current Pywebview Bridge

| Approach | Status | Notes |
|---|---|---|
| Python `pywebview` (current) | ✅ Active | Renders all UI via WebView2 from Python subprocess |
| C++ native WebView2 host | 🔲 Planned | Same HTML/CSS/JS assets — frontend work is not throwaway |

The HTML/CSS/JS files are identical between both approaches. The only difference is whether the WebView2 container is managed by a Python process or directly by the C++ engine.

---

## Frontend Technology Stack

| Layer | Technology | Reason |
|---|---|---|
| UI container | `pywebview` (WebView2) | Native Windows, zero-browser-tab overhead |
| Structure | HTML5 semantic markup | Clean, maintainable |
| Styling | Vanilla CSS with CSS variables (Catppuccin Mocha palette) | Zero build-step, full dark-theme control |
| Logic | Vanilla JavaScript | Zero dependencies, tiny memory footprint |
| Markdown render | `marked.js` (bundled locally) | Renders Gemma hint text in chat bubbles |
| Math render | `KaTeX` (bundled locally) | Renders LaTeX math in hints/reviews |
| Icons | Unicode emoji + Inline SVG | No icon font dependency |

---

## The Four Windows

### 1. 🪲 jugnuBug Mascot (`jugnu_bug.html`)

The always-on, floating bug mascot. Lives permanently in the bottom-right corner of the screen. **Never appears in the taskbar or Alt+Tab switcher** (enforced via Win32 `WS_EX_TOOLWINDOW` style in `launcher.py`).

**State Machine (animations driven by Python stdin JSON commands):**

| State | Trigger | Animation Description |
|---|---|---|
| `sleeping` | Default / idle | Slow idle breathing loop |
| `watching` | App switch to a Focus App | Eyes wide, attentive (15s auto-revert to sleeping) |
| `nudge` | CP_READING_IDLE: User staring at problem | Bug waves / looks curious |
| `thinking` | Gemma is generating a hint | Thought bubble, cogs turning |
| `hint_ready` | Hint generated | Eyes light up, notification pulse |

**State Priority Guard:** A `current_state` tracker in `MascotController` (Python `ipc_client.py`) blocks background events (`SWITCH`, timer expiration) from overwriting high-priority states (`thinking`, `hint_ready`). This prevents the mascot from reverting to `watching` when the user clicks the nudge bubble while Gemma is running.

The mascot's appearance is a Lottie/CSS animation driven by class-switching in the HTML. The mascot's background is transparent (`transparent` CSS on both the root and window background), so it blends natively with the desktop.

**Python → Mascot communication (stdin JSON pipe):**
```json
{"cmd": "set_state", "state": "thinking"}
```

**Mascot → Python communication (stdout JSON pipe):**
```json
{"type": "toggle_dashboard"}
```

---

### 2. 💬 Nudge Bubble (`nudge_bubble.html`)

A small, non-intrusive pop-up that appears near the mascot when the system detects the user has been idle on a CP problem for too long (`CP_READING_IDLE`). Spawned as a separate `subprocess.Popen`.

**Dimensions:** `300 × 110px`
**Position:** Bottom-right, anchored next to the mascot

**Layout:**
```
╔══════════════════════════════╗
║ Still on <slug>?             ║
║ You've been reading... want  ║
║ a starting hint?             ║
║              [Dismiss] [Help]║
╚══════════════════════════════╝
```

**IPC:** The bubble writes JSON events to its own `stdout`, which the Python `ui_listener` thread reads:
- `{"event": "nudge_action", "action": "hint"}` — user clicked "Help"
- `{"event": "nudge_action", "action": "dismiss"}` — user dismissed

When the user clicks "Help", the mascot immediately transitions to `thinking` and the hint pipeline fires.

---

### 3. 📚 Hint Sidebar (`sidebar.html`)

The primary CP assistance panel. Spawned on-demand when Gemma finishes hint generation (`CP_STUCK`) or when the user confirms a hint request from the nudge bubble. Populates from a shared `ui_state.json` state file.

**Dimensions:** `350 × 520px`
**Position:** Bottom-right, above the mascot

**Sidebar Modes (dispatched via `mode` field in `ui_state.json`):**

| Mode | Trigger | Content |
|---|---|---|
| `practice_hint` | Gemma hint generated, `IS_SOLVED: 0` | Hint text + approach badge + past hint history |
| `practice_solved` | Gemma verdict `IS_SOLVED: 1` | Efficiency review text |
| `nudge` | Generic non-CP nudge | Static suggestion |

#### Sidebar Header
Displays:
- 🧠 **Problem Slug** (dynamic, from `ui_state.json`) replaces the generic "Practice Hint" title
- **Badge**: Dynamic `hint_type` from Gemma's `TYPE:` output (e.g., `💡 CONCEPTUAL`, `💡 LOGIC`, `💡 IMPLEMENTATION`) — color-coded:
  - **Blue**: Conceptual / other
  - **Amber**: Logic-related hints
  - **Red**: Implementation / syntax hints
- **Approach summary** (from `APPROACH:` field in Gemma output)
- **Platform tag pill** (e.g., `leetcode`) from `ui_state.json`

#### Chat History
If the current session has prior hints, they are rendered as compact chat bubbles above the new hint, allowing the user to see the full progression of hints within the session.

#### Action Buttons
- ✓ Got it → sends `feedback('helpful')` to Python
- ✗ Not helpful → sends `feedback('not_helpful')` to Python
- ↓ Go Deeper → requests a deeper escalation hint

**Python ↔ Sidebar IPC:**
- Python writes `ui_state.json` before spawning the sidebar subprocess.
- The sidebar reads the file on load via `pywebview.api.get_state()`.
- User actions write JSON events to `stdout`, read by a `_sidebar_feedback_reader` thread.

---

### 4. 📊 Dashboard (`dashboard.html`)

A rich stats panel displaying practice session analytics. Toggled by clicking the jugnuBug mascot itself.

**Dimensions:** `800 × 600px`
**Position:** Center of screen

**Sections:**
- Solved vs. unsolved problems this week
- Recent hint history per problem
- EMA app priority leaderboard
- Quick settings (future)

---

## Visual Design System

All four windows share the same Catppuccin Mocha-inspired CSS variable palette:

```css
:root {
  --bg:       #1e1e2e;   /* main background */
  --surface0: #313244;   /* card surface */
  --surface1: #45475a;   /* borders */
  --text:     #cdd6f4;   /* primary text */
  --subtext1: #bac2de;   /* secondary text */
  --blue:     #89b4fa;   /* CONCEPTUAL hints */
  --green:    #a6e3a1;   /* solved / success */
  --red:      #f38ba8;   /* IMPLEMENTATION hints / errors */
  --yellow:   #f9e2af;   /* LOGIC hints / warnings */
  --mauve:    #cba6f7;   /* accent / badges */

  --radius:    12px;
  --radius-sm: 8px;
  --shadow:    0 8px 32px rgba(0,0,0,0.4);
}
```

All windows use `backdrop-filter: blur(12px)` for the glassmorphism effect.

---

## Launcher Architecture (`launcher.py`)

All four windows are launched by a single `launcher.py` entry-point that accepts a `ui_type` argument:

```
python launcher.py jugnu_bug
python launcher.py nudge_bubble <state_file>
python launcher.py sidebar <state_file>
python launcher.py dashboard <state_file>
```

**Key launcher behaviors:**
- **Taskbar exclusion:** All overlay windows (`jugnu_bug`, `sidebar`, `nudge_bubble`) set `WS_EX_TOOLWINDOW` via a Win32 `ctypes` call after the WebView2 window is created. This removes them from the Taskbar and Alt+Tab switcher.
- **Overlay positioning:** All windows are positioned using `GetSystemMetrics(SM_CXSCREEN/SM_CYSCREEN)` to anchor to screen edges.
- **Slide-in animation:** After the window is positioned, `launcher.py` injects a CSS class via `evaluate_js` to trigger the slide-in animation.

**Sidebar State Read Flow:**
```python
# Python writes state before spawning:
with open(state_file, 'w') as f:
    json.dump(ui_state, f)

# JS inside sidebar reads on load:
const state = await pywebview.api.get_state();
render(state);
```

---

## Animation System

All mascot transitions use CSS `@keyframes` with cubic-bezier easing for premium feel:

```css
@keyframes slideInUp {
  from { opacity: 0; transform: translateY(20px); }
  to   { opacity: 1; transform: translateY(0); }
}
```

Bug idle, thinking, and hint-ready states are distinct CSS animation loops triggered by JavaScript class-swapping in response to the stdin pipe command.

---

## Development Status

| Component | Status |
|---|---|
| jugnuBug mascot (all states) | ✅ Complete |
| Nudge bubble (`CP_READING_IDLE`) | ✅ Complete |
| Hint sidebar (`practice_hint` + `practice_solved`) | ✅ Complete |
| Dashboard (analytics) | ✅ Partially complete (basic stats) |
| Taskbar/Alt-Tab exclusion | ✅ Complete |
| Dynamic hint_type badge coloring | ✅ Complete |
| Chat history in sidebar | ✅ Complete |
| C++ native WebView2 host | 🔲 Planned |
| Full chat UI (`Ctrl+Space` on-demand) | 🔲 Planned |
