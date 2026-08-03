# 🧠 Jugnu — Personalized Study & Coding Buddy for Windows

> A lightweight, always-on AI companion that lives on your Windows laptop, watches what you're doing, remembers your learning journey, and helps you when you get stuck — completely private, mostly offline. Like a firefly (Jugnu), it doesn't drain your battery but lights up when you need it.

---

## 📌 What Is This?

A **personalized AI agent** that runs natively on Windows. Think of it as a study buddy that:
- **Watches** what you're doing across apps via Win32 OS Hooks (VS Code, Chrome, LeetCode, etc.)
- **Understands** the screen structurally — it reads your code and problem statements as separate, labelled sections, not a noisy blob of pixels
- **Remembers** your coding sessions, solutions, and progress using a local vector database of structured knowledge documents
- **Helps proactively** — nudges you when stuck via glassmorphic UI cards, surfaces past context, and tracks your placement prep
- **Stays private** — runs 100% offline using local LLMs (Gemma / Ollama)
- **Zero OS Bloat** — a strictly decoupled C++ telemetry engine handles all OS monitoring; Python only handles AI inference

---

## 🏗️ Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                    C++ Telemetry Engine (Native Daemon)             │
│                                                                     │
│  WinMonitor (SetWinEventHook)                                       │
│     └─ Fires on foreground window change (hDeepWorkEvent)           │
│                                                                     │
│  Hybrid Capture Engine (ScreenReader)                               │
│     └─ Gear 1 (Passive): Fires UIA on tab-switch (10s debounce)     │
│        └─ Writes baseline context & code to SQLite ocr_buffer       │
│     └─ Gear 2 (Active): 60s typing threshold + 5s pause triggers    │
│        a silent Ghost Clipboard (CTRL+C) for pristine code fetch    │
│        └─ Saves pristine code to RAM cache (g_lastCodeBuffer)       │
│                                                                     │
│  StuckTimer Thread                                                  │
│     └─ Monitors AFK/Idle time (3 minutes)                           │
│     └─ Fires USER_IDLE event via Named Pipe IPC                     │
│        └─ Embeds RAM-cached code directly in the JSON payload       │
└─────────────────────────────────────────────────────────────────────┘
                 │                             │
 SQLite ocr_buffer writes              Named Pipe IPC 
 (Baseline Context Path)               (Zero-DB Code Hot-Path)
                 ▼                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Python Inference Engine (uv venv)                │
│                                                                     │
│  FlushWorker (every 60s, AC power only)                             │
│     └─ Consumes ocr_buffer & Area-Wise Dedup (>95% match)           │
│     └─ UIA JSON Path:                                               │
│         Edit controls  → verbatim code, heuristic tag detection     │
│         Document/Text  → Gemma extraction (TOPIC, TAGS, NOTES only) │
│     └─ OKF Synthesis → Writes episodic memories to vector DB        │
│                                                                     │
│  IPC Client Daemon & Practice Engine (practice_mode.py)             │
│     └─ Receives USER_IDLE event + fresh code from C++               │
│     └─ Evaluates active code from IPC against DB context            │
│     └─ Single-Call Gemma Hint Generation (Approach, Type, Hint)     │
│                                                                     │
│  AIEngine (Ollama / Gemma4:e2b)                                     │
│     └─ Situation-Aware Prompting (REPEATED_STRUGGLE)                │
│     └─ Tiered Token Budgeting (prevents VRAM OOM)                   │
│                                                                     │
│  Embedder (multilingual-e5-small)                                   │
│     └─ Deterministic Anchors (exact window_title / file_path match) │
│     └─ Union Merge ("Never Delete, Only Add" scroll-loss fix)       │
│                                                                     │
│  StateManager ─→ Terminal / WebView2 UI                            │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 🚀 What Is Built & Working

### ✅ Socratic Practice Mode (Phase 8)
Jugnu tracks when you are attempting coding problems (e.g., LeetCode) and acts as an empathetic technical interviewer rather than an answer bot. The entire practice mode operates through a strict multi-layer pipeline:
- **Layer 1: Deterministic Context Retrieval:** Uses the exact window title and UIA page content to instantly anchor and retrieve the correct problem description from the vector database, preventing CP problem hallucination.
- **Layer 2: Zero-LLM Active Coding Detection:** A fast, language-agnostic parser distinguishes when you are just reading boilerplates (`CP_READING`) versus actively writing control flow logic (`CP_STUCK`). This prevents premature AI interruptions while you're still reading.
- **Layer 3: Constraint-Aware Correctness Gate:** Before offering a hint, a unified LLM call explicitly verifies if your code solves the problem. Crucially, it mathematically evaluates the Big-O time and space complexity against the problem's stated constraints (e.g. $O(N^2)$ for $N \le 20$), rather than pattern-matching. If correct, Jugnu skips the hint menu entirely (via `CP_SOLVED` routing) and instantly displays an **Efficiency Review**.
- **Layer 4: Unified Socratic Hint Engine & Memory:** If you are truly stuck, the same single LLM call generates the hint. The engine explicitly injects your **Hint History** and **Past Feedback** directly into the prompt using XML delimiters, ensuring Jugnu never repeats itself and remembers what you found helpful. It evaluates your algorithmic approach, spots the flaw, and outputs a 1-2 sentence Socratic question without writing syntax. To prevent false positives, it must explicitly output `IS_SOLVED: 1` to mark a problem as complete.
- **Layer 5: Session State Machine & Lazy DB Writes:** Maintains `practice_sessions` and `practice_hints` in SQLite. Jugnu caches the `is_solved` state in a Python RAM dictionary and completely skips redundant SQLite writes if the problem was already solved in a past session, ensuring zero DB overhead while still providing live feedback.

### ✅ Hybrid Capture Engine & Zero-DB Hot-Paths (Phase 7)
We overhauled the OS telemetry engine to capture pristine data without spamming COM APIs or SQLite:
- **Hybrid Capture (Gear 1 & Gear 2):** C++ operates in two gears. Gear 1 uses a 10-second debounce for passive tab switches, saving the baseline problem statement and initial code to the SQLite `ocr_buffer`. Gear 2 tracks active typing (60s threshold + 5s pause) to fire a silent Ghost Clipboard (CTRL+C), capturing pristine code into RAM without heavy OCR or UIA tree walking.
- **Zero-DB IPC Code Hot-Path:** While the initial page context populates `knowledge_docs` via the standard DB pipeline, your *active keystrokes* bypass the DB entirely. When you're stuck, the C++ StuckTimer sends the RAM-cached code directly to Python over Named Pipes. This guarantees the AI sees your absolute freshest code instantaneously without waiting for the 60s DB flush cycle.
- **Deterministic Anchors & Union Merge:** Bypasses fuzzy vector search for exact window/file anchors and ignores `difflib` deletes (Never Delete, Only Add) to prevent code loss when scrolling in IDEs.

### ✅ Advanced RAG Pipeline (Phase 6)
We overhauled the RAG engine to prevent VRAM crashes and improve answer quality:
- **Blended Re-Ranking & Topic Dedup:** We mathematically mutate vector cosine distance using exponential time decay and logarithmic frequency tracking to surface the *most relevant* memories, while purely discarding identical topic matches.
- **Tiered Token Budgeting:** Slices screen context to 3000 chars, code context to 2500, and supporting docs to 800 to mathematically guarantee it fits inside a strict 8192 token window.
- **Situation-Aware Prompting:** Dynamically swaps the system persona based on telemetry (e.g., if a user has struggled on the same topic 4 times, Jugnu injects a `REPEATED_STRUGGLE` persona to stop giving generic tutorials).
- **JSON Sanitization & Area-Wise Matching:** Strips `\ufffc` null bytes from UIA to prevent llama.cpp stack buffer overflows, and decouples Code vs Prose similarity checks to save GPU time safely.

### ✅ Predictive OS Telemetry & Kernel Pre-warming
Jugnu is not just a passive chatbot; it actively profiles system-level behavioral patterns. Every time you switch windows, the C++ engine updates a **Markov Chain transition matrix** to predict exactly which application you will switch to next. Simultaneously, an **Exponential Moving Average (EMA)** governor tracks the frequency, duration, and priority of your app usage over time. 
**Why this matters:** By understanding your usage habits at the kernel level, Jugnu acts as a predictive system optimizer. It dynamically pre-warms resources for your predicted next app and throttles background distractor apps during deep work. This yields deep analytical insights into user behavior and proves that Jugnu is a deeply integrated Windows telemetry engine, not just a high-level API wrapper.

### ✅ Zero-Overhead Hibernation (C++)
The C++ monitoring threads (`ScreenReader`, `StuckTimer`) do not poll. When the user is outside a work app (gaming, watching a movie), both threads park on `WaitForSingleObject(hDeepWorkEvent, INFINITE)` — consuming **0% CPU**. The `WinEventProc` foreground hook wakes them instantly with a `SetEvent()` call the moment VS Code or Chrome is focused. While active, they sleep for the mathematically exact duration until the next meaningful event, eliminating even mid-interval polls.

### ✅ Structured UIA Extraction & Deduplication (C++)
When the user has been idle for 60 seconds in a work app, `ScreenReader` walks the Windows UI Automation tree with a **Depth-First Search (DFS)** and aggressive **ARIA Pruning**. By checking `get_CurrentIsOffscreen()` and pruning structural nodes (`Pane`, `Group`), it compresses the payload by 90%. The key insight: it distinguishes **code editors** (`Edit` controls) from **prose** (`Document`, `Text` controls) and returns them as separate, labelled JSON objects. Parent-child deduplication prevents a `Document` node from absorbing its child `Text` nodes verbatim. `Edit` nodes (user code) can never be absorbed by prose.

### ✅ Battery-Aware OKF Pipeline & Power Gating (Python)
The Python `FlushWorker` uses the Win32 `GetSystemPowerStatus` API to check if the laptop is plugged into AC power. If on battery, it completely aborts the GPU extraction cycle to save power, purging stale logs older than 10 minutes. When on AC, it parses the structured JSON from C++ and routes each section:
- **Code (`Edit`):** Saved verbatim. Heuristic keyword detection tags it as `C++`, `Python`, `LeetCode`, etc. — no Gemma needed, zero GPU cost.
- **Problem statements (`Document`):** Sent to Gemma for structured extraction.
- **Chrome URL bars:** Silently dropped via a URL heuristic filter.

All sections survive — there is no "best-wins" strategy that discards the user's code in favour of the longer problem statement. Both are saved as independent entries in `knowledge_docs` and indexed in `vec_knowledge` as 384-dimensional vectors.

### ✅ Resilient Safe-Delete Pipeline (Python)
A row is only removed from `ocr_buffer` *after* successful synthesis. If Gemma crashes (OOM, Ollama timeout), the row stays and is retried on the next 60-second cycle. Semantic duplicates are detected via cosine similarity before insert, and merged using LLM if their topics overlap — preventing knowledge vault bloat.

### ✅ Full Memory System (Dual-Table)
Jugnu maintains two parallel memory stores:
- **`episodic_memories` + `vec_episodic`**: Raw session logs. What did you work on and when?
- **`knowledge_docs` + `vec_knowledge`**: Structured, cleaned, semantically indexed knowledge. What did you learn?

Both are stored locally in a single SQLite file with `sqlite-vec` extension, configured with WAL mode to handle concurrent C++ writes and Python reads without `SQLITE_BUSY` contention.

### ✅ Multi-Modal OS Telemetry (Clipboard & Files)
Beyond screen reading, the C++ daemon tracks other context signals natively:
- **Clipboard Monitoring:** Intercepts `WM_CLIPBOARDUPDATE` to instantly capture exact code snippets or error logs you copy, bypassing OCR entirely for perfectly pristine text.
- **File Save Hook:** Uses `ReadDirectoryChangesW` to catch `CTRL+S` events in real-time, signaling to Jugnu exactly which file you consider "ready," acting as a high-priority context trigger.

### ✅ Non-Intrusive UI & Anti-Spam Cooldown
A major problem with AI companions is notification fatigue. Jugnu implements a strict **Cooldown System**: if you decline an idle nudge, it goes completely silent for **15 minutes**. If you accept and get an insight, it sleeps for **20 minutes**. 
Furthermore, the glassmorphic interaction UI spawns via a multiprocess `subprocess.Popen` in a detached PowerShell window, ensuring the main background daemon never blocks while waiting for your input.

---

## 🚧 What Needs Polishing — Gemma Response Quality

While the backend RAG pipeline (retrieval, deduplication, token budgeting, and mathematical re-ranking) is now incredibly robust, **the actual text response generated by Gemma is still not polished yet (we are actively working on that).**

Jugnu currently:
- Successfully retrieves the absolute best `knowledge_docs` using Blended Re-Ranking.
- Injects dynamic Situation-Aware system prompts (`REPEATED_STRUGGLE`, etc.).
- Forces the context into a strictly budgeted token window to prevent OOMs.

**What is not polished yet:**
- **LLM Prose Quality:** Gemma (especially smaller 3-4B variants) sometimes ignores the strict instructions to "be brief" or outputs clunky phrasing despite the high-quality context.
- **Code Hallucinations:** Even when provided with the exact code snippet, small local models sometimes slightly mutate the syntax in their response.
- **Formatting Issues:** The PowerShell CLI output can sometimes mangle the markdown code blocks returned by Gemma.

We are working on refining the few-shot prompting, adjusting temperature parameters, and potentially exploring fine-tunes to make Gemma's final output as pristine as the C++ data pipeline feeding it.

---

## 🔍 Microsoft UI Automation (UIA) — Modified for Code Reading

Jugnu relies heavily on the [Microsoft UI Automation (UIA) API](https://learn.microsoft.com/en-us/windows/win32/winauto/entry-uiauto-win32), which was originally designed by Microsoft for accessibility tools like Windows Narrator to read screens for the visually impaired. 

**How we adapted it for our use case:**
Standard UIA returns an incredibly dense, highly nested tree of every single pixelated button, scrollbar, and invisible pane on the screen. Feeding this raw tree to an AI is too noisy. 
Instead, Jugnu's C++ engine runs a highly optimized **Breadth-First Search (BFS)** across the UIA COM tree:
1. **Targeted Pruning:** We aggressively filter out everything except `Edit` (where the user types code) and `Document`/`Text` (where they read problem statements or docs). 
2. **Parent-Child Deduplication:** UIA often returns a parent `Document` node containing the exact same string as its 5 child `Text` nodes. Jugnu's C++ deduplication pass identifies substring absorption, discarding the redundant children and keeping only the parent.
3. **The Edit Isolation Rule:** Code (`Edit`) nodes are *never* allowed to be absorbed by surrounding prose (`Document`). This guarantees that user code is always extracted flawlessly, maintaining indentation and syntax, completely bypassing OCR guessing.

---

## 📚 The Open Knowledge Format (OKF) — Adapted for LLMs

Jugnu's data architecture is heavily inspired by Google's [Open Knowledge Format (OKF)](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing), an initiative to make data universally readable and structured across systems. 

**1. Generation: Bypassing JSON Hallucinations**
Standard OKF often relies on structured JSON payloads. Initially, we forced our local 4-Billion parameter LLM (Gemma) to output standard JSON. However, when extracting 50 lines of messy C++ code, the LLM almost always hallucinated unescaped quotes (`"`) or broke newline formatting (`\n`), causing fatal `json.loads()` crash loops.

To fix this, Jugnu uses a **Plain-Text OKF Implementation**. We adapted the structured OKF principles into deterministic text headers during generation:

```text
TOPIC:    Binary Search on Rotated Array (LeetCode 33)
TAGS:     C++, LeetCode, binary-search
SUMMARY:  User implemented mid-based search by checking which half is sorted.
CONTENT:  
class Solution { ... }
```
This plain-text schema is completely immune to JSON syntax errors and remains perfectly parseable in Python using simple regex matching.

**2. Storage: From JSON Blobs to Columnar Management**
Initially, we stored this entire parsed OKF document as a single stringified JSON blob in the `knowledge_docs` SQLite table. We quickly realized this was a mistake. Merging new tags meant pulling the whole blob, deserializing it, appending, reserializing, and saving. 

We migrated to strict **Columnar Storage**. After Python parses the plain-text headers, it stores the data in distinct SQLite columns (`ext_topic`, `ext_tags`, `ext_summary`, `ext_content`). 
This decoupled storage allows:
- **Atomic Updates:** We can seamlessly append a new tag or update a topic without touching the heavy content blob.
- **Targeted Embedding:** We only generate a vector embedding for the `ext_summary` column, which acts as a dense semantic anchor, resulting in vastly superior KNN retrieval compared to embedding raw source code.
- **Faster Reads:** The AI engine can pull just the topics and tags for a quick overview without loading megabytes of code into RAM.
---

## 📅 Development Roadmap

### ✅ Phase 1–8 (Completed)
- Full C++/Python dual-process architecture
- Zero-Overhead Hibernation with Win32 Events
- UIA Structured JSON extraction pipeline (DFS + ARIA Pruning)
- OKF Two-Pass synthesis pipeline (Column-Split Schema)
- Resilient `ocr_buffer` safe-delete with retry
- Dual-table memory system (episodic + knowledge)
- WAL-mode SQLite for concurrent access
- Battery-aware FlushWorker (AC power gate + settle time)
- Anti-idle ghost popup trap
- CUDA warmup to prevent KV-cache crash on RTX 4050
- Tiered Token Budgeting & `\ufffc` Null Byte Sanitization
- Situation-Aware Prompt Engineering & Blended Re-Ranking
- Deterministic Vector Anchors & Union Merging
- Ghost Clipboard Native Bypass
- Socratic Practice Engine (State Machine, Hint Escalation, Active Code Heuristics)

### 🔧 Phase 9 — Gemma Response Polishing (In Progress)
- [ ] Refine few-shot prompting to force Gemma to respect brevity instructions
- [ ] Implement strict output parsing to prevent code hallucination mutations
- [ ] Fix PowerShell markdown rendering for code blocks
- [ ] Add Gemini API fallback when the local 4B model lacks confidence

### 🔮 Phase 10 — Native UI & Expansion
- [ ] Port the PowerShell terminal UI to a native C++ WebView2 borderless window

---

## 🖥️ Hardware Requirements

| Component | Minimum | Recommended |
|---|---|---|
| OS | Windows 10 v1903+ | Windows 11 |
| CPU | Any modern Intel/AMD | Intel i5 13th Gen+ |
| GPU | CPU fallback supported | RTX 4050 6GB (full CUDA offload) |
| RAM | 8GB+ | 16GB LPDDR5X |

---

## 🧩 Tech Stack
- **C++20 (MSVC)**: Win32 API, WinRT, `IUIAutomation`, `Windows.Media.Ocr`, `ReadDirectoryChangesW`
- **Python 3**: `uv` package manager, `difflib`, `sqlite3`, `ctypes`, `threading`
- **AI/ML**: `ollama` (Gemma4:e2b local), `sentence-transformers` (multilingual-e5-small)
- **Database**: `sqlite3` + `sqlite-vec` extension (WAL mode, dual-table memory system)

---

*Built with ❤️ for rapid learning, offline privacy, and hyper-optimized OS telemetry.*
