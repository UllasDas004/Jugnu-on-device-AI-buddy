# 🏗️ Jugnu — System Architecture

## Overview

Jugnu is a native Windows desktop AI coding assistant. Its architecture is a **Three-Layer system**: a C++ kernel for real-time OS event capture, a Python inference backend for AI processing, and a multi-window HTML/JS UI rendered by pywebview (WebView2).

---

## The Three Layers

```
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 3: UI Layer (pywebview / WebView2)                           │
│  jugnu_bug.html | sidebar.html | nudge_bubble.html | dashboard.html │
│  stdin/stdout JSON pipes — no HTTP, no sockets                      │
├─────────────────────────────────────────────────────────────────────┤
│  LAYER 2: Python Inference Backend (ipc_client.py)                  │
│  AI Engine (Gemma 4 E2B via Ollama) | Embedder (e5-small-v2)       │
│  FlushWorker | CPEventHandler | StateManager | MascotController     │
├─────────────────────────────────────────────────────────────────────┤
│  LAYER 1: C++ Event Engine (Jugnu.exe)                              │
│  WinMonitor | ScreenReader | CPStateManager | DBHandler | IPCServer │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Layer 1: The C++ Engine

### `WinMonitor` (`win_monitor.cpp`)

The event backbone. Registers `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` with `WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS` so Windows OS calls `WinEventProc` the exact millisecond the foreground window changes.

**Key Behaviors:**

| Behavior | Implementation |
|---|---|
| Deep Work Whitelist | `std::unordered_set` with `code.exe`, `chrome.exe`, `Antigravity IDE.exe`, etc. |
| Hibernation signal | `hDeepWorkEvent` — a manual-reset Win32 event. `ResetEvent()` on non-work app, `SetEvent()` on whitelisted app |
| Jugnu UI focus guard | Detects `python.exe` windows with title starting with `"jugnu"` → sets `g_isJugnuUIFocused` flag, pauses stuck timer |
| Terminal bypass (FIX ST-1) | `WindowsTerminal.exe`, `pwsh.exe`, `cmd.exe` never update `lastMeaningfulApp` |
| Anti-idle ghost popup | `IsUserIdle()` check on every `WinEventProc` call — ignores focus steals while AFK |
| App path harvesting | `GetModuleFileNameExA` extracts each process's absolute `.exe` path and calls `DBHandler::UpsertAppPath` for the RAM Prefetcher |
| Profiling | `QueryPerformanceCounter` measures event processing time — logs warning if > 1ms |

**`StuckTimerThread`:**
- Waits on `hDeepWorkEvent` to hibernate when user is outside the focus zone
- Uses **dynamic math** (no fixed polling): `DWORD timeRemaining = 180000 - idleTime` then `Sleep(timeRemaining)` — precise, no spin-lock
- On 3-minute idle: grabs `ScreenReader::GetLastCodeBuffer()`, JSON-escapes it char-by-char, injects into `USER_IDLE` IPC payload
- `g_isJugnuUIFocused` gate prevents firing while the user is actively clicking the Jugnu UI

---

### `ScreenReader` (`screen_reader.cpp`)

Dual-gear capture engine. Runs on its own thread, hibernates on `hDeepWorkEvent` with 0% CPU.

#### Gear 1 — Tab/Window Switch (10s Debounce)
When the foreground window changes to a whitelisted capture app (VS Code, Chrome, etc.):
1. Wait 10 seconds (debounce to avoid heavy UIA on rapid Alt-Tabs)
2. Check if the user paused typing for > 5 seconds (`extractionPending` flag)
3. Fire `ExtractTextViaUIA(hwnd, allowGhostClipboard=false)` — **NO Ghost Clipboard** during normal UIA to prevent disrupting user
4. Call `DBHandler::SaveToKnowledgeDocs()` — direct to `knowledge_docs` table, bypassing `ocr_buffer`
5. Send `UIA_EXTRACTION_SAVED` JSON event to Python with `row_id` and `is_new` flag

**CP Session Auto-Detection (inside Gear 1):**
- After 10s dwell, window title is checked for `"LeetCode"` or `"Codeforces"`
- If matched: `CPStateManager::StartSession(slug, platform)` is called
- Slug is derived by lowercasing the title and replacing spaces with hyphens

**Abandon Detection (Rage-Quit Catcher):**
- Tracks `lastProblemTitle` (the CP problem window title)
- On any tab change where `lastProblemTitle` is set and the new title doesn't match, sends `PRACTICE_ABANDONED` IPC event to Python

#### Gear 2 — Active Typing Hot-Path (Code RAM Cache)
While the user is actively coding, after 5s typing pause (detected by polling `GetLastInputInfo`):
1. Fire `GhostClipboard(pMonacoEl)` — synthetic click + Ctrl+A + Ctrl+C on the Monaco editor
2. Store the full code string in `g_lastCodeBuffer` (volatile RAM — **not written to DB**)
3. When `StuckTimerThread` fires, it calls `ScreenReader::GetLastCodeBuffer()` and injects the live code directly into the `USER_IDLE` IPC payload

**Why:** FlushWorker only runs every 60s, so DB-sourced code is always stale. The hot-path guarantees 0ms staleness.

#### Ghost Clipboard Protocol (`GhostClipboard()`)
Full implementation detail:
1. Synthetic mouse click at center of Monaco bounding rect (bypasses `SetFocus` which fails on Chrome's shadow DOM)
2. Backup current clipboard contents via `OpenClipboard/GetClipboardData`
3. Set `g_ghostClipboardIgnoreUntilTick` far into the future to suppress our own `WM_CLIPBOARDUPDATE`
4. Send `Ctrl+A` → `EmptyClipboard()` → `Ctrl+C` → wait 150ms for Monaco's async write
5. Read code from clipboard
6. Restore original clipboard atomically
7. Lower ignore tick to `+1000ms` for residual async events
8. Send `VK_RIGHT` to deselect the "Select All" highlight

**Sanity check:** If Ghost Clipboard result is shorter than the UIA partial view (< 90% length), it used the wrong focus target — fall back to UIA text.

#### UIA Extraction Architecture (`ExtractTextViaUIA`)
- **DFS traversal** with an explicit stack (memory-efficient for deep Chrome DOM trees)
- **ARIA landmark pruning**: `complementary` and `contentinfo` regions are pruned entirely (ads, comments, sidebars). `navigation` and `banner` are kept (for Chrome URL bar).
- **RootWebArea URL extraction**: `LegacyIAccessiblePattern::get_CurrentValue()` on the first Document element with a non-empty Name gives the active page URL without requiring omnibox focus
- **PageMeta section**: URL + page title are stored as a combined `{"type":"PageMeta","title":"...","url":"..."}` JSON object. Python splits this for `source_url` storage
- **Content type whitelist**: Only `UIA_EditControlTypeId` (code editors), `UIA_DocumentControlTypeId` (rich text), `UIA_TextControlTypeId` (plain text)
- **Deduplication**: Edit controls are never absorbed by Document controls and vice versa. Substring deduplication collapses redundant nested elements
- **Bounded Levenshtein**: C++ `BoundedSimilarityRatio()` with early-abort at 2% diff threshold — O(N) instead of O(N²) for near-identical captures
- **Sorting**: Edit (code) > Document (page text) by type priority, then by text length descending. Always cap at 5 sections
- **JSON serialization**: Output is a JSON array with `type`, `name`, `full_buffer`, and `text` fields per section

---

### `InputHooks` (`input_hooks.cpp`)

A dedicated Win32 low-level hook thread **exclusively for the CP Practice Mode**. Installed/removed dynamically by `CPStateManager` only when a CP session is active. Runs a `GetMessage` loop on its own thread (0% CPU when idle).

| Atomic | Description |
|---|---|
| `g_lastKeyboardInputMs` | `GetTickCount()` on every real `WM_KEYDOWN` |
| `g_lastMouseInputMs` | `GetTickCount()` on every real mouse event |
| `g_isMouseOnly` | Set if mouse moves but no keyboard for > 2s |
| `g_cpKeyStrokeCount` | Total keystrokes since session start |

**Synthetic input guard:** Both `KbProc` and `MouseProc` check `LLKHF_INJECTED` / `LLMHF_INJECTED` flags and skip events generated by `SendInput` (i.e., from Ghost Clipboard Ctrl+A/Ctrl+C). This prevents the CP state machine from counting our own synthetic inputs as user activity.

On every real `WM_KEYDOWN`: immediately calls `CPStateManager::OnKeyDown()` with zero latency.

---

### `DBHandler` (`db_handler.cpp`)

All SQLite access for C++. Single global connection with thread-safe serialized mode.

**Initialization:**
```cpp
sqlite3_config(SQLITE_CONFIG_SERIALIZED);    // Thread-safe before open
sqlite3_auto_extension(sqlite3_vec_init);     // Register sqlite-vec before open
sqlite3_open_v2(..., SQLITE_OPEN_FULLMUTEX); // Serialize concurrent C++/Python access
sqlite3_busy_timeout(db, 30000);             // 30s retry if Python holds the lock
ExecuteSQL("PRAGMA journal_mode=WAL;");       // P0-FIX: Allow concurrent C++/Python reads
ExecuteSQL("PRAGMA synchronous=NORMAL;");    // ~3x faster than FULL, safe with WAL
```

**Key Tables Created by C++:**

| Table | Purpose |
|---|---|
| `app_priorities` | EMA scores flushed every 30 min from RAM |
| `markov_edges` | App transition counts for Markov chain prediction |
| `app_paths` | Absolute `.exe` paths for RAM prefetching |
| `episodic_memories` + `vec_episodic` | Rolling short-term memory (VIRTUAL table for KNN) |
| `ocr_buffer` | Staging zone for OCR blobs (C++ writes, Python cleans) |
| `knowledge_docs` + `vec_knowledge` | OKF structured long-term memory |
| `practice_sessions` | Per-problem CP session state |
| `practice_hints` | Full hint log per session (type, text, code snapshot, feedback) |

**`SaveToKnowledgeDocs()`:**
- Checks `window_title` for existing row (deterministic deduplication)
- If found: increments `capture_count` and updates `last_updated` only — **never overwrites content or code** (idempotent LTM)
- If not found: inserts new row with `topic='Uncategorized'`
- Returns `row_id` and `isNew` flag for Python IPC notification

---

## Layer 2: The Python Inference Backend

### `MascotController` (`ipc_client.py`)

Holds the `jugnuBug` subprocess. Provides a `set_state(state_name, background_event=False)` API used by every feature.

**State Priority Guard:**
```python
if background_event and self.current_state in ('thinking', 'hint_ready'):
    return   # Don't let background events kill high-priority animations
```

Background events (`SWITCH`, `UIA_EXTRACTION_SAVED`) pass `background_event=True`. Only explicit feature actions (Gemma finished, hint generated) omit the flag.

**Auto-revert:** `watching` state sets a 15-second `threading.Timer` that calls `set_state('sleeping', background_event=True)`. All other states require explicit clearing.

**Subprocess communication:**
- Python → Mascot: `stdin` JSON line: `{"cmd": "set_state", "state": "thinking"}`
- Mascot → Python: `stdout` JSON line: `{"type": "toggle_dashboard"}`

---

### IPC Event Router (`_pipe_reader_daemon`)

Named pipe reader using `PeekNamedPipe` (non-blocking). Runs as a daemon thread, freeing the main thread for `KeyboardInterrupt`.

| IPC Event | Handler |
|---|---|
| `SWITCH` | `state.update_switch()`, mascot `watching` (background) |
| `CLIPBOARD` | `embedder.save_memory()` + OKF synthesis if > 100 chars |
| `FILE_SAVED` | Read file → `save_memory()` + `_synthesize_and_save_file()` |
| `USER_IDLE` | `_idle_handler_background()` in thread — KNN search + Gemma nudge |
| `UIA_EXTRACTION_SAVED` | `flush_worker.process_uia_by_id(row_id)` in thread |
| `CP_SESSION_START` | `cp_handler.handle_session_start()` |
| `CP_SESSION_END` | `cp_handler.handle_session_end()` |
| `CP_READING_IDLE` | `cp_handler.handle_reading_idle()` — nudge bubble + hint |
| `CP_STUCK` | `cp_handler.handle_stuck()` in thread — Gemma code check |
| `CP_USER_RESUMED` | `cp_handler.handle_typing_resumed()` |
| `PRACTICE_ABANDONED` | `cp_handler.handle_session_end()` |

**Code bypass:** `USER_IDLE` and `CP_*` events carry a `"code"` field (the `g_lastCodeBuffer` from C++). If present, Python **skips the SQLite DB read entirely** — guaranteeing 0ms staleness.

---

### `AIEngine` (`ai_engine.py`)

Wraps `ollama.chat()` calls against **Gemma 4 E2B** (`gemma4:e2b`).

**Universal settings:**
- `flash_attn: False` — fixes CUDA PDL crash on RTX 4050 / Turing+ architectures
- `think=False` for all metadata/extraction tasks (speed), `think=True` for `check_code_correctness` (accuracy)
- `num_ctx: 8192` for correctness check, lower for others

**`check_code_correctness(code, content, hint_history, last_feedback)`:**
The critical Practice Mode gate. Uses `think=True` intentionally — Gemma traces code logic step-by-step before rendering a verdict, preventing pattern-match false positives on unusual variable names.

Prompt enforces:
- Code must be **complete + logically correct** — boilerplate or incomplete code is `IS_SOLVED: 0`
- `TYPE:` field extracted for hint categorization (`CONCEPTUAL`, `LOGIC`, `IMPLEMENTATION`)
- `hint_history` injected as `<past_hints>` block to prevent hint repetition

Response parsing:
```
IS_SOLVED: 1 + EFFICIENCY_REVIEW: → efficiency_review dict
IS_SOLVED: 0 + APPROACH + TYPE + HINT → practice_hint dict
```

**`extract_section(text, control_type, cleaned_content)`:**
Gemma extracts only `TOPIC`, `TAGS`, `NOTES` from UIA text — `cleaned_content` (raw C++ UIA payload) is stored directly as `content`, saving output tokens and preventing generation cutoffs.

**`build_rag_context()` Token Budget:**

| Layer | Cap |
|---|---|
| Current screen | 5000 chars |
| Primary doc: code | 4000 chars |
| Primary doc: content | 2500 chars |
| Primary doc: notes | 1500 chars |
| Supporting docs | 1000 chars each |

**Situation-Aware Prompts (`answer_with_context`):**

| Situation | Prompt Persona |
|---|---|
| `STUCK_ON_OWN_CODE` | Bug reviewer: find specific bug in THEIR code vs. documented constraints |
| `REPEATED_STRUGGLE` | Escalated coach: 4+ revisits → direct unblocking insight |
| `READING_NEW_MATERIAL` | Connector: link new docs to active code |
| `CP_READING` | Problem categorizer: 2-sentence approach hint, no code |
| `CP_STUCK` | Socratic interviewer: validate correct parts, ask probing question on bug |
| `GENERAL` / `NO_MEMORY` | Default assistant with memory context |

---

## The IPC Protocol

### C++ → Python (Named Pipe `\.\pipe\jugnu_ipc`)

All payloads are UTF-8 JSON terminated with `"END_OF_MSG\n"`.

```json
{ "type": "SWITCH",   "current_app": "code.exe", "predicted_next": ["chrome.exe"] }
{ "type": "USER_IDLE", "current_app": "code.exe", "code": "<escaped code buffer>" }
{ "type": "UIA_EXTRACTION_SAVED", "row_id": 42, "is_new": true }
{ "type": "CP_SESSION_START", "slug": "two-sum", "platform": "leetcode" }
{ "type": "CP_STUCK", "code": "<escaped code buffer>" }
{ "type": "CP_READING_IDLE", "code": "" }
{ "type": "PRACTICE_ABANDONED", "title": "Two Sum - LeetCode", "code": "..." }
```

### Python → Mascot (stdin pipe)
```json
{"cmd": "set_state", "state": "thinking"}
```

### Python → Sidebar/Nudge (JSON state file)
Written to `ui_state.json` or `nudge_state.json` before subprocess spawn.

---

## The Practice Mode Pipeline

```
[C++ ScreenReader] Tab settled on LeetCode for 10s
    ↓ CPStateManager::StartSession(slug, platform)
    ↓ IPC: CP_SESSION_START

[Python CPEventHandler] handle_session_start()
    ↓ get_or_create_session() → practice_sessions row
    ↓ InputHooks installed (WH_KEYBOARD_LL + WH_MOUSE_LL)

[C++ InputHooks] User reads problem, no keystrokes for 3 min
    ↓ CPStateManager fires CP_READING_IDLE
    ↓ IPC: CP_READING_IDLE + code (from g_lastCodeBuffer)

[Python] handle_reading_idle()
    ↓ Mascot → nudge state
    ↓ Spawn nudge_bubble.html

[User clicks Help] nudge_bubble stdout: {"event":"nudge_action","action":"hint"}
    ↓ Mascot → thinking state
    ↓ engine.check_code_correctness(code, problem_context, hint_history)
    ↓ log_hint() → practice_hints DB row (with real hint_type from Gemma TYPE: field)
    ↓ Write ui_state.json
    ↓ Spawn sidebar.html
    ↓ Mascot → hint_ready state

[User starts coding] InputHooks detect keystrokes
    ↓ CPStateManager fires CP_USER_RESUMED
    ↓ Python: handle_typing_resumed() — dismisses sidebar

[C++ ScreenReader Gear 2] 5s pause after 60s+ of typing
    ↓ GhostClipboard() → g_lastCodeBuffer updated

[3 min idle while coding] StuckTimerThread fires
    ↓ IPC: CP_STUCK + live code from g_lastCodeBuffer
    ↓ Python: handle_stuck() in background thread
    ↓ engine.check_code_correctness() with think=True
    ↓ Sidebar updated with hint or efficiency review
```

---

## SQLite Concurrency Architecture

WAL mode enables concurrent C++ writes and Python reads without `SQLITE_BUSY` errors:

```
C++ Thread (WinMonitor / ScreenReader)     Python FlushWorker Thread
    ↓ DBHandler::SaveToKnowledgeDocs()          ↓ sqlite3.connect() 
    ↓ PRAGMA journal_mode=WAL                   ↓ read knowledge_docs
    ↓ busy_timeout=30000ms                      ↓ process_uia_by_id()
    → Non-blocking concurrent access ←→→→→→→→→→
```

The `sqlite-vec` extension is registered via `sqlite3_auto_extension` **before** `sqlite3_open_v2`. The `.h` file is never included to avoid the macro redefinition trap — only the `extern "C"` forward declaration is used.
