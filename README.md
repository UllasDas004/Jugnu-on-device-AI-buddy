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
│     └─ Fires on every foreground window change                      │
│     └─ Manages hDeepWorkEvent (manual-reset Win32 Event)            │
│                                                                     │
│  ScreenReader Thread (hibernates on hDeepWorkEvent)                 │
│     └─ Wakes ONLY when user is in a whitelisted work app            │
│     └─ Extracts via UIA → Structured JSON [{type, name, text}]      │
│     └─ Falls back to WinRT OCR if UIA returns nothing               │
│     └─ Writes result to SQLite ocr_buffer (zero IPC overhead)       │
│                                                                     │
│  StuckTimer Thread (hibernates on hDeepWorkEvent)                   │
│     └─ Fires USER_IDLE event to Python after 3 min AFK              │
│                                                                     │
│  App Switch → Named Pipe IPC → Python StateManager                  │
└─────────────────────────────────────────────────────────────────────┘
                                │
              SQLite ocr_buffer │ Named Pipe IPC
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Python Inference Engine (uv venv)                │
│                                                                     │
│  FlushWorker (every 60s, AC power only)                             │
│     └─ Reads ocr_buffer rows settled for >30s                       │
│     └─ Deduplicates per-section via difflib (MAX_CACHE=20)          │
│     └─ UIA JSON Path:                                               │
│         Edit controls  → verbatim code, heuristic tag detection     │
│         Document/Text  → Gemma extraction (section-aware)           │
│     └─ OCR Fallback: context-aware 500-char chunks → Gemma          │
│     └─ Synthesis → OKF knowledge_doc                                │
│     └─ Embedder: cosine dedup check → insert or LLM merge           │
│     └─ Deletes row only after successful synthesis (retry on crash) │
│                                                                     │
│  AIEngine (Ollama / Gemma4:e2b)                                     │
│     └─ extract_section() for Document/Text controls                 │
│     └─ combine_sections() → OKF knowledge_doc                       │
│     └─ generate_answer() for RAG responses                          │
│                                                                     │
│  Embedder (multilingual-e5-small)                                   │
│     └─ save_knowledge_doc() → vec_knowledge + knowledge_docs        │
│     └─ save_memory()        → vec_episodic + episodic_memories      │
│                                                                     │
│  StateManager ─→ ipc_client ─→ Terminal / WebView2 UI              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 🚀 What Is Built & Working

### ✅ Predictive OS Telemetry & Kernel Pre-warming
Jugnu is not just a passive chatbot; it actively profiles system-level behavioral patterns. Every time you switch windows, the C++ engine updates a **Markov Chain transition matrix** to predict exactly which application you will switch to next. Simultaneously, an **Exponential Moving Average (EMA)** governor tracks the frequency, duration, and priority of your app usage over time. 
**Why this matters:** By understanding your usage habits at the kernel level, Jugnu acts as a predictive system optimizer. It dynamically pre-warms resources for your predicted next app and throttles background distractor apps during deep work. This yields deep analytical insights into user behavior and proves that Jugnu is a deeply integrated Windows telemetry engine, not just a high-level API wrapper.

### ✅ Zero-Overhead Hibernation (C++)
The C++ monitoring threads (`ScreenReader`, `StuckTimer`) do not poll. When the user is outside a work app (gaming, watching a movie), both threads park on `WaitForSingleObject(hDeepWorkEvent, INFINITE)` — consuming **0% CPU**. The `WinEventProc` foreground hook wakes them instantly with a `SetEvent()` call the moment VS Code or Chrome is focused. While active, they sleep for the mathematically exact duration until the next meaningful event, eliminating even mid-interval polls.

### ✅ Structured UIA Extraction & Deduplication (C++)
When the user has been idle for 60 seconds in a work app, `ScreenReader` walks the Windows UI Automation tree with a BFS traversal, extracting up to 5 meaningful sections. The key insight: it distinguishes **code editors** (`Edit` controls) from **prose** (`Document`, `Text` controls) and returns them as separate, labelled JSON objects. Parent-child deduplication prevents a `Document` node from absorbing its child `Text` nodes verbatim. `Edit` nodes (user code) can never be absorbed by prose.

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

## 🚧 What Needs Polishing — RAG Response Quality

The hardest part of Jugnu — **intelligently using the stored knowledge during a response** — is still in early stages and needs significant work.

Jugnu currently:
- Can retrieve the top-K relevant `knowledge_docs` via vector similarity search when the user asks a question.
- Can pull the user's active `ocr_buffer` / screen context as a prompt prefix.
- Generates a response by stitching this context into the Gemma prompt.

**What is not polished yet:**
- **Context ranking**: All retrieved docs are treated equally. There is no re-ranking by recency, relevance score, or session proximity. A LeetCode solution from 3 weeks ago carries the same weight as one seen 5 minutes ago.
- **Knowledge retrieval at response time**: The `generate_prompt_context()` function in `ai_engine.py` fetches RAG docs but the integration back into the final prompt is minimal. The retrieved knowledge docs are not always used meaningfully in the answer.
- **The "idle nudge" full loop**: Jugnu correctly detects when the user is stuck and sends them a notification. But the actual answer generation (Pass 2 of RAG) is not consistently pulling the freshest, most relevant `knowledge_docs` for the current problem.
- **Multi-doc synthesis**: When multiple `knowledge_docs` are retrieved (e.g. both the problem statement and the user's past code), they are concatenated naively. A smarter merge or guided prompt is needed.

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

### ✅ Phase 1–5 (Completed)
- Full C++/Python dual-process architecture
- Zero-Overhead Hibernation with Win32 Events
- UIA Structured JSON extraction pipeline
- OKF Two-Pass synthesis pipeline
- Resilient `ocr_buffer` safe-delete with retry
- Dual-table memory system (episodic + knowledge)
- WAL-mode SQLite for concurrent access
- Battery-aware FlushWorker (AC power gate + settle time)
- Anti-idle ghost popup trap
- CUDA warmup to prevent KV-cache crash on RTX 4050

### 🔧 Phase 6 — RAG Response Polishing (In Progress)
- [ ] Re-rank retrieved `knowledge_docs` by recency + similarity score before injecting into the prompt
- [ ] Fix the idle-nudge full loop: retrieved docs should always match the current screen context
- [ ] Multi-doc synthesis: merge retrieved docs intelligently instead of naive concatenation
- [ ] Properly use the freshest `ocr_buffer` text (current screen) as hard context over older `knowledge_docs`

### 🔮 Phase 7 — Native UI & Cloud Escalation
- [ ] Port the PowerShell terminal UI to a native C++ WebView2 borderless window
- [ ] Add Gemini API fallback when the local 4B model lacks confidence

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
