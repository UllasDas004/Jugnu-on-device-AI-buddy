# 🚀 Jugnu — Features Roadmap

## ✅ What's Built Right Now (Phases 1–8)

---

### Phase 1 — Core IPC + C++ Event Engine
- `WinMonitor` with `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` — zero-poll OS integration
- Named Pipe IPC server (`\\\\.\\pipe\\jugnu_ipc`) for C++ → Python messaging
- EMA Priority Map + Markov Chain in C++ RAM (flushed every 30 min to SQLite)
- Deep Work Whitelist gating — only whitelisted apps wake the monitoring threads
- Anti-idle ghost popup trap — `IsUserIdle()` filter on all foreground events
- Terminal bypass (FIX ST-1) — terminals never overwrite the meaningful app context
- App path harvesting — `GetModuleFileNameExA` saves `.exe` paths for RAM prefetching

---

### Phase 2 — Python Inference Backend
- `StateManager` + `Embedder` (e5-small-v2 via SentenceTransformers)
- `AIEngine` (Gemma 4 E2B via Ollama) with warmup retry loop
- `FlushWorker` — background processor of `ocr_buffer` every 60s (AC power gated)
- SWITCH / CLIPBOARD / FILE_SAVED / USER_IDLE IPC event routing
- Non-blocking `PeekNamedPipe` reader daemon (main thread stays free for Ctrl+C)

---

### Phase 3 — OKF Memory System
- `knowledge_docs` + `vec_knowledge` (sqlite-vec KNN) for structured long-term memory
- `ocr_buffer` staging table — C++ writes raw OCR, Python cleans with Gemma
- Deterministic anchor deduplication — `window_title` / `file_path` match before vector search
- "Never Delete, Only Add" union merging policy for scroll-loss code preservation
- OCR-to-UIA upgrade path — cleaner UIA text overwrites dirty OCR entries
- Area-wise deduplication — Edit (code) and Document (page) controls tracked separately

---

### Phase 4 — Ghost Clipboard
- Full Ctrl+A + Ctrl+C Ghost Clipboard implementation
  - Synthetic mouse click at Monaco bounding rect (bypasses `SetFocus` Chrome shadow DOM failure)
  - User clipboard backup & atomic restore
  - `g_ghostClipboardIgnoreUntilTick` guard suppresses own `WM_CLIPBOARDUPDATE` events
  - VK_RIGHT deselect after extraction
- `g_ghostClipboardIgnoreUntilTick` + `LLKHF_INJECTED` guards prevent synthetic input counting

---

### Phase 5 — Advanced UIA + KNN RAG
- DFS UIA traversal with ARIA landmark pruning (`complementary`, `contentinfo` pruned)
- RootWebArea URL extraction via `LegacyIAccessiblePattern` (no address bar focus needed)
- `PageMeta` section type — title + URL stored and split by Python
- `BoundedSimilarityRatio` — O(N) bounded Levenshtein with 2% early abort
- Blended KNN Re-Ranking: `final_score = distance - (0.12 × recency_bonus) - (0.08 × frequency_bonus)`
- Layer-3 Topic Deduplication in KNN results (85% similarity threshold)
- Tiered Token Budget RAG context builder (5000/4000/2500/1500/1000 char caps)

---

### Phase 6 — Zero-DB IPC Code Hot-Path (Gear 2)
- `g_lastCodeBuffer` volatile RAM cache — updated on every 5s typing pause
- Ghost Clipboard on Gear 2 path — stores full Monaco buffer directly to RAM (no DB write)
- `StuckTimerThread` injects `g_lastCodeBuffer` directly into `USER_IDLE` IPC payload
- Python `_pipe_reader_daemon` bypasses SQLite DB read when `ipc_code` is present
- Guarantees 0ms code staleness vs. up to 59s stale from the FlushWorker path

---

### Phase 7 — CP Practice Mode v1
- `CPStateManager` — C++-side state machine (IDLE → READING → CODING → STUCK)
- `InputHooks` — dedicated `WH_KEYBOARD_LL` + `WH_MOUSE_LL` hooks for CP session only
  - `LLKHF_INJECTED` filter prevents synthetic Ghost Clipboard inputs from counting
  - `g_cpKeyStrokeCount`, `g_lastKeyboardInputMs`, `g_isMouseOnly` atomics
- `practice_sessions` + `practice_hints` SQLite tables (created by C++ `db_handler.cpp`)
- `CP_SESSION_START`, `CP_SESSION_END`, `CP_READING_IDLE`, `CP_STUCK`, `CP_USER_RESUMED` IPC events
- Platform and slug auto-detection from window title in C++ ScreenReader
- Rage-quit / abandon detection — `PRACTICE_ABANDONED` IPC event on tab switch from CP problem

---

### Phase 8 — Practice Mode v2 + UI Polish + WAL Concurrency Fix
- **Combined correctness gate** (`check_code_correctness`):
  - `think=True` for accurate code logic tracing (prevents pattern-match false verdicts)
  - Binary `IS_SOLVED` flag with strict parser — no hallucinated efficiency reviews
  - Constraint-aware prompting — evaluates Big-O against problem's stated constraints
  - `<past_hints>` block in prompt — prevents hint repetition
  - `TYPE:` field parsed and stored in `practice_hints.hint_type` correctly
- **Glassmorphic sidebar** (`sidebar.html`) with:
  - Dynamic `hint_type` badge coloring (Blue/Amber/Red)
  - Chat history scrollback (prior hints in the session as chat bubbles)
  - Problem slug in header, platform tag pill
  - Approach text from Gemma's `APPROACH:` field
- **MascotController priority guard** — `background_event=True` flag prevents SWITCH/timer events from overwriting `thinking`/`hint_ready` states
- **WAL mode** — `PRAGMA journal_mode=WAL` + `busy_timeout=30000` eliminates `database is locked` crashes
- **UIA direct-to-`knowledge_docs`** fast path — bypasses `ocr_buffer` for tab-switch captures
- **Dashboard** (`dashboard.html`) — CP stats with per-session drill-down, hint history, strategy heatmap

---

## 🔲 What's Next

### Phase 9 — Gemma Response Polishing
- [ ] Socratic hint escalation tiers (conceptual → partial code → full guided walkthrough)
- [ ] Approach confidence scoring improvements
- [ ] Hint feedback loop — use `user_feedback` to adjust future hint style

### Phase 10 — Onboarding & Settings
- [ ] First-run onboarding window — set up focus apps, model, preferences
- [ ] Settings panel (pause monitoring, model selection, clear DB)
- [ ] `user_preferences` table in SQLite

### Phase 11 — C++ Native WebView2 Host
- [ ] Migrate from `pywebview` subprocess to native C++ WebView2 (`ICoreWebView2`)
- [ ] Same HTML/CSS/JS assets — no frontend rework needed
- [ ] Win32 message loop integration for zero-latency JS ↔ C++ bridge

### Phase 12 — Advanced Features (Original Roadmap)
- [ ] **Proactive Buddy Cards**: Semantic cosine similarity vs. Error Anchor vector triggers nudge
- [ ] **Flow State Enforcer**: Alt-Tab to distraction domain → buddy card intervention
- [ ] **File System Ghost-Writer**: `ReadDirectoryChangesW` + `FILE_ACTION_MODIFIED` → auto-summarize saved files
- [ ] **Alt-Tab Predictor**: Markov chain preloads VS Code → Jugnu context before user switches
- [ ] **Drag & Drop Local RAG**: PyWebView HTML5 drop → PDF/ZIP chunking → `document_vault`
- [ ] **Audio Fading**: `IAudioEndpointVolume` — lower system volume 20% on stuck nudge
- [ ] **RAM Prefetching**: `GetAppPath()` + `CreateFile(FILE_FLAG_SEQUENTIAL_SCAN)` → force app into OS page cache
- [ ] **Codeforces Support**: Extended slug detection + contest-mode CP session handling
