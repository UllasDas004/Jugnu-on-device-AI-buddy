# 🏗️ Jugnu — System Architecture

This document describes the architectural decisions behind Jugnu — what we built, why we built it this way, and how each design choice is more efficient than what came before.

---

## Core Philosophy

> **Pure algorithms for systems intelligence. ML strictly for language understanding.**

Every routing, caching, prioritization, and deduplication decision is handled by deterministic algorithms — not neural networks. The LLM (Gemma) only fires when the user needs natural language: when they ask a question or when text must be semantically understood.

The architectural boundary is simple and absolute:
- **C++** owns everything that must be always-on, zero-latency, and system-level.
- **Python** owns everything that involves a neural network, a prompt, or an embedding.
- They share state through a single SQLite database (WAL mode) and communicate lightweight telemetry over Windows Named Pipes.

---

## Why C++ + Python, Not Pure Python?

Early prototypes ran the entire monitor loop in Python. The problem was immediate: Python's GIL (Global Interpreter Lock) meant that a long Gemma inference call would momentarily freeze the clipboard monitor and miss events. Clipboard captures were dropped, app-switch timestamps were skewed.

The split fixes this permanently. The C++ engine is a native Windows process — it hooks into the OS event loop directly and is physically incapable of being blocked by Python inference. Python becomes a completely independent process that can take as long as it needs without affecting data collection.

---

## The Three-Layer Architecture

```
┌──────────────────────────────────────────────────┐
│         C++ ENGINE  (jugnu.exe)                  │
│  Always-on. Event-driven. Zero-overhead.         │
│  • App switch tracking (WinEventHook)            │
│  • Screen capture (IUIAutomation + WinRT OCR)    │
│  • Stuck timer (3-min idle detection)            │
│  • SQLite database (shared memory vault)         │
│  • DSA memory systems (Markov + EMA)             │
│  • Named Pipe IPC server                         │
└──────────────────┬───────────────────────────────┘
                   │ Windows Named Pipes
                   │ (lightweight telemetry only)
┌──────────────────▼───────────────────────────────┐
│     PYTHON INFERENCE SERVICE (ipc_client.py)     │
│  Heavy lifting. RTX 4050 GPU. Runs independently.│
│  • Gemma 3 via Ollama (LLM inference)            │
│  • e5-small ONNX (384-dim text embeddings)       │
│  • FlushWorker (background OKF pipeline)         │
│  • Notification bridge (PowerShell UI)           │
└──────────────────┬───────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────┐
│         NATIVE UI (Microsoft WebView2)           │
│  Floating, borderless. Summoned via Ctrl+Space.  │
└──────────────────────────────────────────────────┘
```

---

## The Zero-Overhead Hibernation System

### The Old Approach and Why It Failed

The original stuck timer and screen reader both ran on a simple `while(true) { Sleep(10s); check(); }` loop. This had two fatal problems:

1. **Power drain**: The threads woke up every 10 seconds unconditionally — even while the user was watching a movie or playing a game. This burned battery and CPU doing absolutely nothing useful.
2. **False positives**: The stuck timer would fire during video playback because `GetLastInputInfo` was idle for 3 minutes — the user wasn't stuck, they were watching a tutorial.

### The New Approach: `hDeepWorkEvent`

We replaced the polling loop with a native Windows Kernel Event object (`hDeepWorkEvent`). This is a zero-cost synchronization primitive built into the Windows kernel.

The `WinEventHook` in `win_monitor.cpp` — which fires on every foreground app switch — now acts as the master switch:

- When the user switches to a **Focus App** (VS Code, Cursor, Chrome, IntelliJ, etc.), the hook signals the event.
- When they switch to anything else (Spotify, YouTube, a game), the hook resets the event.

Both the Screen Reader and the Stuck Timer thread simply wait on this event before doing any work. When it is not signaled, the Windows scheduler puts these threads into a true **kernel sleep** — consuming exactly 0% CPU, 0 battery drain. The system is completely inert until the user sits down to code.

This means Jugnu is not just "low CPU" — it is **physically incapable of doing any computation** while the user is not in a focused session.

---

## App Switch Detection (`win_monitor.cpp`)

The monitor registers a `WinEventHook` with the OS for the `EVENT_SYSTEM_FOREGROUND` event. This is a pure callback — the OS itself calls our function the instant the foreground window changes. No polling. No timers. Zero overhead between app switches.

On each switch, the monitor:
1. Resolves the new process name from the window handle
2. Decides if it is a Focus App and signals/resets `hDeepWorkEvent` accordingly
3. Updates the **Markov Chain** (records the transition for app prediction)
4. Updates the **EMA priority score** for the app (learns which apps the user values most)

---

## Screen Capture (`screen_reader.cpp`)

### The Three-Tier Capture Strategy

When the user has been idle for 60 seconds inside a Focus App, the Screen Reader captures context. It tries three strategies in order, falling back to the next if the previous yields nothing.

**Tier 1: IUIAutomation (UIA)**

UIA is Windows' native accessibility framework — the same one used by screen readers for visually impaired users. Instead of taking a screenshot and running it through OCR, UIA reads the actual text directly from the application's UI tree.

This is architecturally superior in every way:
- The text is **perfect** — no OCR noise, no misread characters, no hallucinated symbols.
- It preserves **code indentation** exactly.
- It captures **multiple independent sections** — the code editor block and the problem description block are extracted as separate items with their control type (`Edit`, `Document`, `Text`) attached.
- It works **without GPU** — pure COM call, zero image processing.

The UIA engine collects all candidate sections, deduplicates parent-child redundancy (a section that contains a smaller section's text absorbs it), and serializes the result as a structured JSON array. Python receives typed sections — it knows instantly if something is code or documentation.

**Tier 2: WinRT OCR (Fallback)**

For apps that don't expose a UIA accessibility tree, the screen reader falls back to a GPU-accelerated screenshot pipeline. It captures the window pixels using GDI, converts them to a WinRT SoftwareBitmap, and passes them to the native Windows 10/11 OCR engine. This runs on dedicated hardware (the GPU's media engine) and is significantly faster than Python-based Tesseract or EasyOCR.

**Tier 3: Skip**

If a process is not in the Focus App list, capture is skipped entirely. This is enforced by an O(1) hash set lookup — no linear string comparisons.

### Why Write to SQLite Instead of Sending Over Named Pipes?

The old design sent OCR text blobs directly over the Named Pipe to Python. This had a fatal scaling problem: a 4K monitor can produce 6,000–8,000 characters of text. Sending this over a byte stream meant Python's IPC reader had to buffer, receive, and parse a massive JSON string — blocking the entire event loop for hundreds of milliseconds and causing dropped telemetry.

The new design completely eliminates this bottleneck. C++ writes the captured text directly to the `ocr_buffer` SQLite table using native C APIs. This is an asynchronous disk write that takes under 1 millisecond. The Named Pipe is now only used for lightweight signals (clipboard events, file saves, stuck alerts). Python's background `FlushWorker` reads the `ocr_buffer` at its own pace, on its own schedule, without ever touching the IPC loop.

---

## The Stuck Timer

The Stuck Timer is a dedicated thread that detects when the user has been idle for 3 minutes inside a Focus App. It signals a `USER_IDLE` event to Python via Named Pipe, which then triggers Gemma to generate a proactive suggestion.

**Key design decisions:**

- **Completely gated on `hDeepWorkEvent`**: The timer does not run at all outside Focus Apps. There is no risk of it triggering while the user is watching a video.
- **3-minute window with reset**: Every time the user switches into a Focus App, the timer resets. The clock only runs while they are actively in a session.
- **Single-shot per idle period**: The alert fires once per idle period. Once the user moves their mouse or types, `hasOcredWhileIdle` resets, and the next idle period starts fresh.

---

## The OKF Knowledge Pipeline (Python)

### The Problem with Raw Context

Early designs tried to store raw screen text as memories and retrieve them via vector search. This produced terrible RAG results because:

1. **e5-small-v2 is a sentence embedding model** — it performs well on natural prose, poorly on raw code symbols, indentation, and mixed UI noise.
2. **Raw OCR text is dirty** — scrollbar ratios, menu labels, button text, and status bars all contaminate the semantic space.
3. **Duplicate captures** — the same problem or code file might be captured 20 times across sessions, flooding the vector store with near-identical entries.

### The Objective Knowledge Format (OKF)

The OKF pipeline transforms dirty, raw screen captures into clean, structured knowledge documents with distinct fields: a topic, a prose summary, explanatory content, a verbatim code snippet, semantic tags, and a capture count.

The pipeline has four distinct stages:

**Stage 1 — Routing by Control Type**

Because C++ now attaches the UIA control type to each captured section, Python can route immediately:
- `Edit` controls (code editors): flagged as verbatim and bypass Gemma entirely. A zero-GPU heuristic function (`_detect_code_tags`) scans the text for language-specific keywords and attaches the correct language tag instantly.
- `Document` / `Text` controls (problem statements, docs): sent to Gemma for structured extraction.

This is the most important optimization in the pipeline. Previously, every captured section went through Gemma regardless. Now, the majority of content (code) completely bypasses the LLM — cutting GPU time by roughly 60-80% for typical coding sessions.

**Stage 2 — Gemma Extraction (Document/Text only)**

Gemma reads the raw document text and outputs structured key-value headers (`TOPIC:`, `TAGS:`, `NOTES:`, `CONTENT:`). There is no JSON in the output — this was a deliberate decision after LLM markdown hallucination crashes. When Gemma was asked to produce JSON, it would occasionally wrap the output in markdown code fences or hallucinate trailing commas, crashing `json.loads()`. Plain header lines are parsed with simple string splitting — robust against any hallucination.

**Stage 3 — Pure Python Section Fusion**

All extracted sections are merged using pure Python logic: tags are deduplicated as a set, paragraphs are deduplicated using `difflib` string similarity, and code snippets are kept as the longest unique version. No LLM is involved at this stage.

**Stage 4 — Semantic Anchor Embedding**

A critical insight: the `e5-small-v2` model works best on natural prose. Instead of embedding the full content (which may include hundreds of lines of code), we ask Gemma to generate a 1-2 sentence prose summary describing what the knowledge document is about. Only this summary and the topic are embedded and stored in `vec_knowledge`. The retrieval quality compared to embedding raw text is dramatically better.

---

## The Deduplication + Merge Strategy

When a new knowledge document is about to be saved, `embedder.py` first checks if a semantically similar document already exists in `vec_knowledge` using KNN cosine similarity. If the cosine distance is less than 0.03 (greater than 97% similar), the documents are considered about the same topic.

**The old approach** was to feed both the existing and new document into Gemma and ask it to merge them. This wasted GPU time, was slow (~2-3 seconds per merge), and occasionally hallucinated content.

**The new approach** is entirely mathematical:
- Code snippets: keep the longer one (longer = more complete implementation)
- Content paragraphs: `difflib` line-by-line comparison, drop near-duplicate lines
- Tags: Python set union
- `capture_count` incremented: this field becomes a natural importance signal — a topic seen 15 times is demonstrably more important than one seen once

Zero GPU. Zero LLM. Pure Python running in milliseconds.

---

## The SQLite Memory Vault

All persistent state lives in a single SQLite database with WAL (Write-Ahead Logging) mode enabled. WAL allows C++ and Python to hold simultaneous read connections without blocking each other, and Python writes do not block C++ reads.

The database is organized into tiers by permanence:

| Tier | Table | Permanence | Who writes |
|------|-------|-----------|------------|
| 0 (hot) | C++ RAM maps | Volatile — 30-min flush | C++ (always) |
| 0.5 (staging) | `ocr_buffer` | Cleared every cycle | C++ writes, Python clears |
| 1 (episodic) | `episodic_memories` + `vec_episodic` | Rolling 5,000-row cap | Python on events |
| 2 (knowledge) | `knowledge_docs` + `vec_knowledge` | Never evicted, only merged | Python FlushWorker |
| 3 (persona) | `core_persona` | Permanent | Written at onboarding |

Knowledge documents are never deleted or evicted — they only grow richer through merging. The episodic log rolls over (oldest rows pruned) to keep the database size bounded. The OKF vault is the long-term brain.

---

## RAG Context Assembly

When the user asks a question, context is assembled in two steps:

1. **Search Query Generation**: Gemma reads the current screen context (what the user is looking at right now) and generates a dense 15-word semantic query that captures the core technical problem. This is significantly more accurate than using the user's raw question as a search query, because the screen context reveals what they are actually working on.

2. **Vector Search**: The 15-word query is embedded and searched against `vec_knowledge` using KNN cosine similarity. The top 5 matching OKF documents are retrieved — these may contain the exact LeetCode problem the user was solving yesterday, or the exact code pattern they were studying last week.

The answer is then grounded in the user's own past learning history, not general knowledge.

---

## Responsibility Split (Exact Boundary)

| Component | Language | Reason |
|---|---|---|
| App switch detection | C++ | OS callback — must be always-on |
| IUIAutomation capture | C++ | COM API — native, zero-overhead |
| WinRT OCR fallback | C++ | WinRT — native GPU hardware path |
| `ocr_buffer` writer | C++ | Direct SQLite C API — sub-millisecond |
| Markov Chain | C++ | O(1) hash map — trivial math |
| EMA priority scoring | C++ | Float arithmetic — trivial math |
| LRU Cache | C++ | DSA — hot path |
| Named Pipe IPC server | C++ | Zero-latency secure bridge |
| 30-min RAM→SQLite flush | C++ | Always running background thread |
| Clipboard monitor | C++ | Win32 API |
| File system watcher | C++ | Win32 `ReadDirectoryChangesW` |
| `hDeepWorkEvent` gate | C++ | Kernel event — gates all background threads |
| Stuck timer | C++ | Thread gated on `hDeepWorkEvent` |
| Gemma inference | Python | Ollama SDK — trivial, easy to update prompts |
| e5-small embedding | Python | ONNX Runtime — trivial |
| OKF extraction pipeline | Python | FlushWorker — reads `ocr_buffer`, writes `knowledge_docs` |
| Semantic anchor embedding | Python | Only embeds 1-2 sentence prose summary |
| Mathematical OKF merging | Python | `difflib` — zero GPU, milliseconds |
| RAG context assembly | Python | Queries `vec_knowledge`, builds prompt |
| Stuck alert UI | Python | PowerShell prompt via subprocess |

---

## Background Task Schedule

| Task | Interval | Language | Purpose |
|---|---|---|---|
| EMA + Markov flush | 30 min | C++ | Persist in-RAM state to SQLite |
| OKF Batch Cleaner | 60 sec (AC only) | Python | Process `ocr_buffer` → `knowledge_docs` |
| Async file synthesis | On file save | Python daemon | Extract OKF doc from saved code file |
| Gemma idle unload | 5 min no queries | Python | Free ~2.7GB VRAM when not in use |
| EMA decay | Nightly | C++ | Decay unused apps — enforces 0.1 floor |
| Markov pruning | Weekly | C++ | Remove transition edges with count = 1 |
