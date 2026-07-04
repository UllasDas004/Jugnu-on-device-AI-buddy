# 🏗️ Jugnu — System Architecture

This document provides a conceptual overview of the Jugnu hybrid architecture.

## Language Architecture Decision: C++ Engine + Python Inference Service

> **TL;DR:** C++ handles everything that must be always-on, low-latency, and system-level. Python handles everything that involves AI models. The two communicate seamlessly via local HTTP.

### Why Not Pure C++?
Running AI models (like Gemma) directly inside C++ is extremely complex, rigid, and hard to update. By moving the model inference to a Python background service, we can use standard, industry-grade libraries (`llama-cpp-python` and `onnxruntime`) while keeping the C++ engine tiny and lightning-fast. Communication happens instantly via Windows Named Pipes (`\\.\pipe\jugnu_ipc`).

### The MSVC & WinRT Decision
In Phase 3, we strictly migrated the C++ engine to **MSVC (Visual Studio Build Tools)** instead of MinGW. This was done to unlock native Windows Runtime (WinRT) APIs. Things like OCR (`Windows.Media.Ocr`) and Graphics Capture are now executed in pure C++ on the GPU, completely eliminating the need for slow Python `subprocess` shell scripts.

---

## The Hybrid Architecture Flow

```text
┌─────────────────────────────────────────────────────────────────────┐
│                     C++ ENGINE (jugnu.exe)                          │
│                                                                     │
│  The Master Controller. Always-on, low-latency, < 1% CPU.           │
│                                                                     │
│  • Win32 System Hooks (Tracks app switches & active windows)        │
│  • Screen Readers (UI Automation & OCR)                             │
│  • SQLite Database (The Memory Vault)                               │
│  • DSA Memory Systems (Markov Chain & EMA App Scoring)              │
│  • WebView2 Host (Renders the floating UI popup)                    │
│                                                                     │
└─────────────────────────┬───────────────────────────────────────────┘
                          │  Internal Localhost API (Invisible to user)
                          │  (C++ to Python IPC via Windows Named Pipes)
┌─────────────────────────▼───────────────────────────────────────────┐
│               PYTHON INFERENCE SERVICE (inference.py)               │
│                                                                     │
│  The Heavy Lifter. Runs models on the RTX 4050 GPU.                 │
│                                                                     │
│  • llama-cpp-python (Runs the Gemma 3-4B model)                     │
│  • ONNX Runtime (Runs the e5-small text embedding model)            │
│  • Background FlushWorker (Cleans OCR data into vector memory)      │
│  • Gemini API Fallback (For broad web knowledge)                    │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────────┐
│                   NATIVE UI (Microsoft WebView2)                    │
│              Floating, borderless Windows App (HTML/CSS/JS)         │
│              Summoned via Ctrl+Space hotkey                         │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Responsibility Split (Exact Boundary)

| Component | Language | Why |
|---|---|---|
| WinEventHook app monitor | **C++** | Win32 API, callback-based, must be always-on |
| UI Automation (IUIAutomation) | **C++** | COM API, Windows-native, zero-overhead |
| WGC + Windows OCR | **C++** | WinRT C++, hardware GPU path |
| SQLite + sqlite-vec | **C++** | Always-on, embedded, direct memory access |
| Markov Chain (unordered_map) | **C++** | O(1) DSA, hot path, no ML needed |
| EMA priority (float arithmetic) | **C++** | Trivial math, hot path |
| LRU cache | **C++** | DSA, hot path |
| Frontend HTTP server (:7331) | **C++** | Serves web UI to WebView2 |
| Named Pipe IPC Server | **C++** | Zero-latency, secure C++ ↔ Python comms |
| Periodic flush thread | **C++** | Background thread, always running |
| OCR batch cleaner | **Python** | flush_worker.py (runs every 60s on AC power) |
| Clipboard monitor | **C++** | Win32 API |
| File system watcher | **C++** | Win32 API (ReadDirectoryChangesW) |
| Audio monitor | **C++** | WASAPI COM API |
| **Gemma inference (llama.cpp)** | **Python** | 3 lines vs 150 lines. No contest. |
| **e5-small embedding (ONNX)** | **Python** | ort.InferenceSession — trivial |
| **Gemini API fallback** | **Python** | google-generativeai SDK |
| **All 4 onboarding prompts** | **Python** | Easy to iterate, change prompts without rebuild |
| **Nightly fact extraction** | **Python** | Scheduled task, prompt engineering |
| **RAG prompt assembly** | **Python** | String manipulation + context injection |

---

## Boot Sequence

1. `jugnu.exe` starts.
2. C++ launches `inference.py` silently in the background.
3. C++ initializes COM apartments (required for UI Automation and WebView2).
4. C++ initializes the SQLite Database and loads the Markov/EMA memory maps into RAM.
5. C++ checks if Onboarding is complete. If not, it opens the WebView2 window in Onboarding mode.
6. C++ attaches hooks to the Windows OS to listen for App Switching (`EVENT_SYSTEM_FOREGROUND`).
7. Background flush thread starts (saving RAM to SQLite every 30 mins).
8. Standard Windows Message Loop runs, listening for the `Ctrl+Space` hotkey.

---

## File Structure

```text
jugnu/
│
├── CMakeLists.txt              ← C++ build config
├── vcpkg.json                  ← C++ dependencies (nlohmann_json)
├── requirements.txt            ← Python dependencies (llama-cpp-python, onnxruntime)
│
├── src/                        ← C++ Engine (MSVC Native)
│   ├── main.cpp
│   ├── core/
│   │   ├── db_handler.h/.cpp
│   │   ├── lru_cache.h/.cpp
│   │   └── memory_manager.h/.cpp
│   ├── monitor/
│   │   ├── win_monitor.h/.cpp
│   │   ├── screen_reader.h/.cpp ← Native WinRT OCR Engine
│   │   ├── clipboard_monitor.h/.cpp
│   │   ├── fs_watcher.h/.cpp
│   │   └── audio_monitor.h/.cpp
│   ├── server/
│   │   └── ipc_server.h/.cpp   ← Windows Named Pipes server
│   └── privacy/
│       └── privacy_guard.h/.cpp
│
├── inference/                  ← Python Inference Service
│   ├── inference_service.py    ← Named Pipes client (main entry point)
│   ├── flush_worker.py         ← Background OCR cleaner and embedder
│   ├── embedder.py             ← e5-small ONNX wrapper
│   ├── generator.py            ← Gemma llama-cpp-python wrapper
│   ├── gemini_client.py        ← Gemini API fallback
│   ├── prompts/
│   │   ├── onboarding.py       ← All 4 onboarding prompts
│   │   ├── extraction.py       ← Nightly fact extraction prompt
│   │   ├── keywords.py         ← Keyword expansion prompt
│   │   └── rag.py              ← RAG prompt assembly + context injection
│   └── nightly/
│       └── nightly_job.py      ← Scheduled nightly extraction job
│
├── frontend/                   ← Web UI (HTML/CSS/JS for WebView2)
│   ├── index.html
│   ├── css/style.css
│   └── js/
│       ├── chat.js
│       ├── dashboard.js
│       └── settings.js
│
├── sqlite3/                    ← Bundled (C++ only)
│   ├── sqlite3.h, sqlite3.c, sqlite3ext.h
│   └── sqlite-vec.h, sqlite-vec.c
│
└── models/
    ├── gemma-3-4b-it-Q4_K_M.gguf
    ├── multilingual-e5-small.onnx
    └── README.md
```

---

## Python Dependencies (requirements.txt)

```
llama-cpp-python[cuda]    # llama.cpp with CUDA support for RTX 4050
onnxruntime               # e5-small embedder (CPU)
google-generativeai       # Gemini API fallback
numpy                     # Vector math
transformers              # Tokenizer for e5-small (tokenize only, not inference)
```

**Install:**
```bash
# Install llama-cpp-python with CUDA support (RTX 4050)
CMAKE_ARGS="-DLLAMA_CUDA=on" pip install llama-cpp-python[cuda]

# Rest of deps
pip install google-generativeai numpy transformers onnxruntime
```

---

## Core Philosophy (Unchanged)

> **Pure DSA for systems intelligence. Python ML for language understanding.**
>
> The boundary is clear: if it involves an `unordered_map`, a mutex, a Win32 API, or SQLite — it's C++. If it involves a neural network, a prompt, or an API call — it's Python. The two never cross this line.

### C++ Pillar: Memory Engine (Pure DSA)

```
What happened?  → Second-Order Markov Chain (O(1) lookup)
How important?  → Exponential Moving Average — EMA (float, per app)
What to keep?   → EMA-aware LRU Cache (evict lowest EMA first)
Where to store? → SQLite with periodic flush (30-min interval)
What's on screen? → UI Automation + WGC+OCR (3-tier pipeline)
```

### Python Pillar: Language Engine (ML)

```
What does text mean?     → e5-small ONNX embedding (384-dim vector)
What is user asking?     → Gemma 3-4B via llama-cpp-python (~1-2s)
Is fact already known?   → Cosine search via C++ sqlite-vec, not Python
Need broad knowledge?    → Gemini API (opt-in, per-query consent)
```

---

## Module Breakdown

### C++: `win_monitor.cpp` — The Eyes of the System

```
EVENT_SYSTEM_FOREGROUND fires →
  GetWindowThreadProcessId() → PID →
  GetModuleFileNameEx() → "code.exe" →
  GetWindowText() → "solution.py — Visual Studio Code" →
  Parse: fileName="solution.py", language="Python", topic="LeetCode" →
  updateMarkov(prevApp, currApp, timeBlock) →     ← pure C++ DSA
  updateAppPriority(appName) →                   ← pure C++ DSA
  screen_monitor.CaptureAndOCR(hwnd) →           ← C++ GPU OCR
  DBHandler::BufferOCR(app, text) →              ← Saves to ocr_buffer SQLite table
  (Later: flush_worker.py extracts code via Gemma and embeds it)
```

**Time Block (same as Synapse, unchanged):**
```cpp
std::string getTimeBlock() {
    SYSTEMTIME st; GetLocalTime(&st);
    if (st.wHour >= 5  && st.wHour < 9)  return "EarlyMorning";
    if (st.wHour >= 9  && st.wHour < 12) return "Morning";
    if (st.wHour >= 12 && st.wHour < 17) return "Afternoon";
    if (st.wHour >= 17 && st.wHour < 20) return "Evening";
    if (st.wHour >= 20 && st.wHour < 24) return "Night";
    return "LateNight";
}
```


---

### C++: `webview_ui.cpp` — The Native Floating Window

| Action | Who calls it | What it does |
|---|---|---|
| `WebView2::Create` | `main.cpp` | Spawns a native Win32 window hosting Chromium |
| `HotKey (Ctrl+Space)` | User | Toggles visibility of the Jugnu window |
| `JS ↔ C++ bridge` | WebView2 | HTML UI sends JSON directly to C++ without HTTP |

**Chat flow (The user experience):**
```
User hits Ctrl+Space → Jugnu native window pops up instantly
User types "what was I working on yesterday?"
  → WebView2 passes text directly to C++
  → C++: embed query via Python /embed → get query vector
  → C++: searchSimilar(vector, 10) → SQL fetch from episodic_log
  → C++: getAllCoreFacts(3) → SQL fetch from core_persona
  → C++: POST Python /generate {prompt, episodic_context, persona_facts}
  → Python: Gemma inference (~1-2s)
  → C++: streams response back into WebView2 JS engine
```

---

## Background Tasks

| Task | Runs in | Interval | What it does |
|---|---|---|---|
| Periodic flush | C++ thread | 30 min | Write EMA + Markov from RAM → SQLite |
| OCR batch cleaner | Python thread | 60 sec | Gemma extracts tech data from `ocr_buffer` (AC power only) |
| Async File Synthesis | Python daemon | On File Save | Synthesizes massive code files into OKF documents without blocking IPC |
| Gemma idle unload | Python | 5 min no queries | Unload model from VRAM (free 2.7GB) |

---

## Python AI Orchestration Pipeline

The Python layer is highly optimized for performance and defensive programming to avoid blocking the C++ kernel pipeline.

1. **`ipc_client.py`**: The bridge to C++. It reads the Named Pipe using `win32file.ReadFile`. It strictly separates telemetry ingestion (main thread) from heavy file synthesis (daemon thread `_synthesize_and_save_file`).
2. **`ai_engine.py`**: Manages the Ollama inference. Features a cold-start `_warmup()` 1-token query to absorb CUDA crashes, completely disables Flash Attention for mobile GPU stability, and uses a two-pass `extract_ocr_chunk` → `synthesize_ocr_extractions` pipeline to build structured OKF JSON docs.
3. **`flush_worker.py`**: The background OCR sweeper. It uses `ctypes` to check `GetSystemPowerStatus` and aborts if on battery. It forces a 30-second settle time before reading `ocr_buffer` and uses `difflib.SequenceMatcher` to skip Gemma extraction entirely if the screen hasn't changed >85%.
4. **`embedder.py`**: The vector store interface. Prevents fatal HuggingFace API network timeouts during offline usage by pinging `8.8.8.8` and gracefully falling back to `local_files_only=True`. Connects to SQLite with `timeout=5.0` to avoid crashing when C++ executes a massive `BEGIN TRANSACTION` lock.
5. **`notification.py`**: The UI bridge. Uses `subprocess.CREATE_NEW_CONSOLE` to spawn an interactive PowerShell prompt over the user's IDE instantly, preventing the main IPC loop from blocking on `input()`. Features the "Custom Problem Override" — throwing away pre-fetched screen context if the user asks a manual, unrelated question.
| Nightly extraction | Python (Task Scheduler) | 2 AM | Fact extraction → core_persona |
| EMA decay | Python (nightly) | Nightly | Apply decay to priority_map |
| Row limit | C++ (nightly trigger) | Nightly | Trim episodic_log to 5,000 |
| Markov prune | C++ (nightly trigger) | Weekly | Remove count=1 transitions |


## Core Philosophy

> **Pure DSA for systems intelligence. ML strictly for language understanding.**
>
> Every routing, caching, prioritization, and deduplication decision is made by algorithms — not neural networks. This is what makes the system fast, predictable, and memory-efficient. ML (Gemma + e5-small) only fires when the user needs natural language — i.e., when they ask a question or the system needs to understand text meaning.

---

## The Two Pillars

### Pillar 1: Memory Engine (C++ — Pure DSA)

```
What happened? → Second-Order Markov Chain (O(1) lookup)
How important? → Exponential Moving Average — EMA (float, per app)
What to keep?  → EMA-aware LRU Cache (evict lowest EMA first)
Where to store? → SQLite with periodic flush (30-min interval)
```

### Pillar 2: Language Engine (ML)

```
What does this text mean? → e5-small ONNX embedding (384-dim vector)
Is it an error or docs?   → Cosine similarity against "Anchor Vectors" in C++ (Zero NLP battery drain)
What is the user asking?  → Gemma 3-4B via llama.cpp CUDA (~1-2s response)
Is this fact already known? → Cosine similarity search in sqlite-vec
Need broad knowledge?     → Gemini API (opt-in only, per-query consent)
```

---

## Module Breakdown

### `win_monitor.cpp` — The Eyes of the System

**Replaces:** Android `AccessibilityService`  
**Primary API:** `SetWinEventHook` (Win32)

```
EVENT_SYSTEM_FOREGROUND fires → 
  Get foreground HWND →
  GetWindowThreadProcessId() → PID →
  OpenProcess() + GetModuleFileNameEx() → process name (e.g., "code.exe") →
  GetWindowText() → window title (e.g., "solution.py — VS Code") →
  Parse title → extract file name, language, topic →
  updateMarkov(prevApp, currApp, timeBlock) →
  updateAppPriority(appName) →
  Run screen_monitor.readWindow(hwnd)
```

**Time Block Calculation (same as Synapse):**
```cpp
// Converts local time to one of 6 blocks
// Used as the 3rd dimension in the Markov state key
std::string getTimeBlock() {
    SYSTEMTIME st;
    GetLocalTime(&st);
    if (st.wHour >= 5  && st.wHour < 9)  return "EarlyMorning";
    if (st.wHour >= 9  && st.wHour < 12) return "Morning";
    if (st.wHour >= 12 && st.wHour < 17) return "Afternoon";
    if (st.wHour >= 17 && st.wHour < 20) return "Evening";
    if (st.wHour >= 20 && st.wHour < 24) return "Night";
    return "LateNight";
}
```

**Window Title Parser (new — no Android equivalent):**
```cpp
struct WindowContext {
    std::string processName;    // "code.exe"
    std::string windowTitle;    // "solution.py — Visual Studio Code"
    std::string fileName;       // "solution.py"
    std::string language;       // "Python" (from extension)
    std::string app;            // "VS Code"
    std::string topic;          // "LeetCode" / "study" / "design"
    bool isOnCall;              // Zoom/Teams/Meet running with audio
    bool isRecording;           // OBS running
};
```

---

### `inference_router.cpp` — Local vs Cloud Routing

```
User sends a query
       │
       ▼
  Is it deeply personal?
  (contains "I", "my", "last time", "remember", "what did I")
       │
    YES│                          NO
       ▼                           ▼
  Use Gemma (local)          Is it broad factual?
  Build RAG context           (history, science, math)
  from episodic_log +               │
  core_persona                   YES│
  → llama.cpp CUDA               ▼
  → ~1-2s response         Show user: "This needs internet.
                            Use Gemini API? [Yes / No]"
                                  │
                               YES│
                                  ▼
                            Gemini API call
                            with privacy-scrubbed prompt
                            (no personal facts sent)
```

**llama.cpp CUDA Config (RTX 4050, Q4_K_M):**
```cpp
llama_context_params params;
params.n_gpu_layers = 99;    // Full model offload (all 26 Gemma layers)
params.n_ctx = 4096;         // Context window
params.use_mmap = true;      // Memory-mapped model file

// Idle unload: if no query for 5 minutes, unload model from VRAM
// Reloads in ~3 seconds on next query
// Frees 2.7GB VRAM for gaming/other apps
```

**RAG Prompt Template (adapted from Synapse):**
```
<start_of_turn>system
You are Jugnu, a personal study and coding companion.
You know this user personally from their stored memories.

Core facts about this user:
{core_persona_facts}   ← top 3 from vector search

Recent context (last 2 hours):
{episodic_context}     ← top 7 from vector search

Current activity:
App: {current_app} | Window: {current_window_title}
File: {current_file}
<end_of_turn>

<start_of_turn>user
{user_query}
<end_of_turn>

<start_of_turn>model
```

---

### `webview2.cpp` & `ipc_server.cpp` — The Communication Layer

Because the UI is hosted natively via Microsoft WebView2, we **do not need a local HTTP server**. The frontend communicates directly with the C++ backend using a JSON bridge (`window.chrome.webview.postMessage`). 

When C++ needs the Python inference service, it uses **Windows Named Pipes** (`\\.\pipe\jugnu_ipc`).

**Communication Flow:**
```
Frontend UI (JS) 
    │ (WebView2 JSON Bridge)
    ▼
C++ Engine (jugnu.exe)
    │ (Windows Named Pipes)
    ▼
Python Service (inference.py)
```

---

## System Boot Sequence

```
1. main.cpp starts
2. CoInitializeEx(COINIT_APARTMENTTHREADED)  ← COM init for UI Automation
3. db_handler.init() → SQLite open + WAL mode + create tables
4. db_handler.loadPriorityMap() → restore EMA from disk
5. db_handler.loadTransitionMatrix() → restore Markov from disk
6. memory_manager.init()
7. Check config.json → ONBOARDING_DONE?
   ├── NO  → Open WebView2 Onboarding UI
   └── YES → Continue
8. screen_monitor.init() → CoCreateInstance(CLSID_CUIAutomation)
9. win_monitor.init() → SetWinEventHook(EVENT_SYSTEM_FOREGROUND)
10. clipboard_monitor.init() → AddClipboardFormatListener()
11. fs_watcher.init() → ReadDirectoryChangesW(coding_folder)
12. audio_monitor.init() → IMMDeviceEnumerator (WASAPI)
13. embedder.init() → ONNX Runtime session (e5-small)
14. ipc_server.init() → Open Named Pipe for Python
15. webview2.init() → Open floating Jugnu UI
16. Start background flush thread (30-min interval)
17. Win32 message loop → GetMessage / TranslateMessage / DispatchMessage
```

---

## Onboarding System (4-Prompt Design — Ported from Synapse)

The onboarding runs directly inside the Jugnu Native WebView2 window.

### 4 Prompts (identical logic to Synapse's conversational_onboarding_architecture)

| Prompt | Purpose | Temp | User sees? |
|---|---|---|---|
| **Prompt 1** | Warm conversation, adaptive archetype detection | 0.7 | ✅ Yes |
| **Prompt 2** | Silent fact extraction (`FACT:` format) | 0.0 | ❌ No |
| **Prompt 3** | Per-fact keyword expansion (5-10 keywords) | 0.0 | ❌ No |
| **Prompt 4** | Final cross-fact holistic pass (20-40 keywords) | 0.0 | ❌ No |

### Archetype Detection (Windows-Specific Additions)

| Archetype | Detection Signals | Deep-Drill Questions |
|---|---|---|
| Student / Placements | "DSA", "LeetCode", "sem", "college", "FAANG" | Subjects? Which companies? DSA progress? |
| Software Dev (work) | "job", "sprint", "PR", "company", "stack" | Stack? Remote/office? Team size? |
| Content Creator | "edit", "Premiere", "reel", "thumbnail", "OBS" | Software? Platform? Niche? |
| Researcher | "paper", "thesis", "LaTeX", "lab" | Domain? Institution? |
| Designer | "Figma", "wireframe", "prototype", "Canva" | Tools? Code too? Clients? |
| Entrepreneur | "startup", "clients", "revenue", "product" | Stage? Solo/team? B2B or B2C? |

### Windows-Specific Onboarding Questions
```
"Which IDE or editor do you mainly use?"
  → VS Code → offer VS Code extension integration

"Is Chrome or Edge your main browser?"  
  → Chrome → offer Chrome DevTools Protocol tab tracking

"Where does most of your code live on your machine?"
  → Answer → set up ReadDirectoryChangesW watcher for that path

"Do you usually work with music or in silence?"
  → Music → Spotify audio = focus signal
  → Silence → any audio = distraction signal
```

### Output Written to `config.json`
```json
{
  "onboarding_done": true,
  "persona_keywords": ["dynamic programming", "placement prep", "Python", "LeetCode"],
  "coding_folder": "D:\\coding\\",
  "preferred_browser": "chrome",
  "vscode_extension_installed": false,
  "audio_preference": "music_ok",
  "monitoring_level": "full",
  "blocked_apps": [],
  "blocked_domains": []
}
```

---

## Background Tasks

| Task | Interval | What it does |
|---|---|---|
| **Periodic flush** | 30 min | Write EMA + Markov from RAM to SQLite |
| **Nightly extraction** | 2 AM (on-charger) | LLM fact extraction from authored episodic_log |
| **EMA decay** | Nightly | Decay unused apps, enforce 0.1 floor |
| **Row limit enforcement** | Nightly | Trim episodic_log to 5,000 best rows |
| **Markov pruning** | Weekly | Remove transition_matrix entries with count=1 |
| **Model idle-unload** | 5 min idle | Unload Gemma from VRAM, free 2.7GB |

---

## Memory Architecture (adapted from Synapse V17)

```
Tier 0: In-RAM unordered_map (hot, instant, volatile)
         ↓ flush every 30 min
Tier 1: SQLite on disk (persistent, survives reboot)
         ↓ nightly LLM pass
Tier 2: core_persona (permanent facts, never evicted)
```

**SIGKILL survival (same as Synapse Trap E-1):**
- C++ RAM `unordered_map` → LOST on crash
- SQLite WAL mode → survives anything
- 30-min flush = max 30 min data loss
- On next boot: `loadPriorityMap()` + `loadTransitionMatrix()` restores full state


## Phase 1.5 Updates (Interim PyWebView Architecture)
- **Hybrid UI Bridge**: Integrated `pywebview` as an interim frontend host before full C++ WebView2 migration. Allowed reusing HTML/CSS/JS assets without native compilation overhead during rapid prototyping.
- **Thread-Safe IPC Rendering**: Separated the Named Pipe polling loop (main thread) from the `pywebview` notification trigger logic (daemon thread). Used `threading.Event().wait()` to block UI loops without stalling IPC telemetry.
- **GhostWriter Aggressive Filtering**: Modified the Win32 `ReadDirectoryChangesW` hooking mechanism to surgically ignore metadata, `.venv`, `uv.lock`, and build artifacts, preventing exponential feedback loops when dependencies are updated.

---

## Module Deep-Dive: `file_watcher.cpp` — The GhostWriter

This module watches a directory for file saves and streams the absolute path to Python over the Named Pipe.

### How `ReadDirectoryChangesW` Works Internally

The function is a **blocking kernel syscall**. The thread calls it and is immediately put to sleep by the Windows Scheduler (0% CPU). When any file inside the watched directory is modified, the kernel wakes the thread and fills a buffer with an array of `FILE_NOTIFY_INFORMATION` structs packed together.

```cpp
// The BLOCKING call — thread sleeps here until the kernel wakes it
ReadDirectoryChangesW(
    hDir,              // Handle to the open directory
    buffer,            // Output buffer to fill with notifications
    sizeof(buffer),    // Buffer size — must be large enough or events are LOST
    TRUE,              // Watch recursively into all subdirectories
    FILE_NOTIFY_CHANGE_LAST_WRITE, // Only trigger on saves, not creates/deletes
    &bytesReturned,    // How many bytes were written to the buffer
    NULL, NULL         // No async overlapped I/O — synchronous mode
);
```

### Walking the Linked-List-by-Offset

Windows packs multiple events into one buffer as a contiguous linked list. Each `FILE_NOTIFY_INFORMATION` struct has a `NextEntryOffset` field which is the **byte distance** to the next struct. If it's 0, we're at the end.

```cpp
FILE_NOTIFY_INFORMATION* fni = reinterpret_cast<FILE_NOTIFY_INFORMATION*>(buffer);
do {
    // Process current fni...
    
    // Walk to next: raw byte pointer arithmetic
    fni = fni->NextEntryOffset
        ? reinterpret_cast<FILE_NOTIFY_INFORMATION*>(
              reinterpret_cast<char*>(fni) + fni->NextEntryOffset)
        : nullptr;
} while(fni);
```
This is a classic **intrusive linked list** — the 'next pointer' is not an actual pointer but a byte offset, because the kernel allocates everything in one flat buffer.

### Wide String (UTF-16) to UTF-8 Conversion

Windows natively stores filenames as UTF-16 (wide strings, `wchar_t`). `FILE_NOTIFY_INFORMATION.FileName` is a `WCHAR[]` array. We must convert it:

```cpp
std::wstring wFilename(fni->FileName, fni->FileNameLength / sizeof(WCHAR));
std::string filename(wFilename.begin(), wFilename.end());
```
The division by `sizeof(WCHAR)` (which is 2 bytes) converts the byte count to a character count. The range constructor `std::string(wFilename.begin(), wFilename.end())` does a naive cast — this is fine for ASCII paths but would corrupt non-ASCII unicode in filenames. A production fix would use `WideCharToMultiByte(CP_UTF8, ...)` like the `clipboard_manager.cpp` does.

### The JSON Backslash Escape Loop

This was a real production bug we hit. Windows paths use `\` separators. JSON requires `\\`. Without escaping, Python's `json.loads()` throws `JSONDecodeError`.

```cpp
std::string escapePath = absolutePath;  // D:\coding\jugnu\inference\ai_engine.py
size_t pos = 0;
while((pos = escapePath.find("\\", pos)) != std::string::npos)
{
    escapePath.replace(pos, 1, "\\\\");  // \ becomes \\
    pos += 2;  // CRITICAL: skip past the 2 chars just inserted, not 1!
}
```
The `pos += 2` is the key insight: after replacing 1 char (`\`) with 2 chars (`\\`), if we only advance by 1, the loop finds its own `\\` and re-escapes it into `\\\\` forever — an infinite loop eating all RAM.

### The `FILE_FLAG_BACKUP_SEMANTICS` Mystery

To open a **directory** handle using `CreateFileA` (normally used for files), you must pass `FILE_FLAG_BACKUP_SEMANTICS`. This is a poorly documented Windows quirk. The flag was originally added to let backup software open directories with full access. Without it, `CreateFileA` returns `INVALID_HANDLE_VALUE` with error code `5 (Access Denied)` when given a directory path — even as Administrator.

```cpp
HANDLE hDir = CreateFileA(
    watchPath.c_str(),
    FILE_LIST_DIRECTORY,
    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
    NULL, OPEN_EXISTING,
    FILE_FLAG_BACKUP_SEMANTICS,  // The magic flag that enables directory handles
    NULL
);
```
