# 🧠 Jugnu — Personalized Study & Coding Buddy for Windows

> A lightweight, always-on AI companion that lives on your Windows laptop, watches what you're doing, remembers your learning journey, and helps you when you need it — completely private, mostly offline. Like a firefly (Jugnu), it doesn't drain your battery but lights up when you're stuck in the dark.

---

## 📌 What Is This?

A **personalized AI agent** that runs natively on Windows. Think of it as a study buddy that:
- **Watches** what you're doing across apps via Win32 OS Hooks (VS Code, Chrome, etc.)
- **Remembers** your study sessions, coding patterns, struggles, and progress using a local Vector Database
- **Helps proactively** — nudges you when stuck via glassmorphic UI cards, surfaces past context, and tracks your placement prep
- **Stays private** — runs 100% offline using local LLMs (Gemma / Ollama)
- **Zero OS Bloat** — A strictly decoupled C++ telemetry engine streams data via Named Pipes to a sandboxed Python UI/AI engine.

---

## 🏗️ Architecture Overview

```mermaid
graph TD
    subgraph "C++ Telemetry Engine (Background Daemon)"
        W[WinMonitor] -->|WinEventHooks| AppSwitch[App & Idle Tracking]
        FW[FileWatcher] -->|ReadDirectoryChangesW| FileMonitor[File Saves]
        CB[Clipboard] -->|WM_CLIPBOARDUPDATE| ClipTracker[Copy/Paste]
        SR[ScreenReader] -->|UIAutomation / OCR| TextCapture[UIA Fast-Path + WinRT OCR]
    end

    subgraph "Named Pipe IPC"
        AppSwitch -->|JSON Stream - Overlapped I/O| Pipe((jugnu_ipc))
        FileMonitor -->|JSON Stream| Pipe
        ClipTracker -->|JSON Stream| Pipe
        TextCapture -->|Direct SQL Dump / IPC| OCRBuffer[(ocr_buffer DB)]
    end

    subgraph "Python Inference & UI Server (uv venv)"
        Pipe -->|Listener| StateManager[State Aggregator]
        OCRBuffer -->|Batch Clean| FlushWorker[FlushWorker Daemon]
        FlushWorker -->|Pass 1: Extractor| AIEngine[Local Ollama / Gemma]
        AIEngine -->|Pass 2: Synthesizer| OKF[OKF Document]
        OKF -->|Clean Text| Embedder[embedder.py]
        StateManager -->|Direct Text| Embedder
        Embedder -->|vec_knowledge| SQLiteVec[(sqlite-vec DB)]
        StateManager -->|Lazy Context| AIEngine
        StateManager -->|Trigger| TerminalUI[PowerShell jugnu_interact.py]
    end
```

---

## 🚀 What It Does Right Now (Phase 5 Completed)
Jugnu has successfully completed **Phase 4.5 & 5: Dynamic Governor & IPC Hardening**.
- **Dynamic Process Governor**: Uses an Exponential Moving Average (EMA) map to intelligently throttle background distractor apps during deep work sessions, completely eliminating hardcoded process lists and protecting critical OS services.
- **Section-Wise RAG Synthesis**: Hardened the Python OCR pipeline against CUDA stack overflows by processing massive 20,000-character screen captures in bounded chunks. Guarantees long problem statements and short code snippets are both successfully captured and merged into the vector database.
- **Resilient Memory Persistence**: SQLite Write-Ahead Logging (WAL) handles concurrent database locks, while a custom `SetUnhandledExceptionFilter` guarantees the last 30 minutes of learned RAM state is forcibly flushed to disk before any fatal process crashes.
- **The C++ GhostWriter**: Deep Win32 hooks actively track Window switching, idle time, and File Saves with zero OS bloat.
- **UIA Fast-Path & OCR Engine**: For known text-heavy apps (VS Code, Chrome), Jugnu instantly traverses the accessibility DOM via `IUIAutomation` for 100% pristine text extraction, bypassing heavy GPU workloads. For other apps, it silently falls back to capturing `BitBlt` screenshots and extracting text via `Windows.Media.Ocr` on the GPU.
- **Non-Blocking IPC**: Uses Asynchronous Overlapped I/O with `WaitForMultipleObjects` to stream JSON telemetry and `===SECTION===` UIA blocks over Named Pipes without ever deadlocking the C++ engine.
- **Lazy RAG Evaluation**: To save massive amounts of battery, Jugnu no longer generates 500-token answers in the background when you go idle. It performs a hyper-fast (<100ms) vector search and completely defers GPU generation until you explicitly click "Yes" on the UI notification.
- **Battery-Aware Pipeline**: The `FlushWorker` daemon enforces a 30-second screen settle time and uses `difflib.SequenceMatcher`. Clean UIA text completely bypasses the Gemma LLM de-noising pass, saving immense GPU compute.
- **LLM Markdown Hallucination Bypass**: Extraction prompts strictly output `TOPIC:/TAGS:/CODE:` plaintext headers, cleanly circumventing the classic JSON decoder crash loops that plague open-source LLMs.
- **Async File Synthesis**: Saving massive 8,000+ line code files pushes synthesis tasks to detached `daemon=True` background threads, preventing C++ IPC buffer overflows.
- **Custom Problem RAG Override**: If the UI pops up based on a Python file, but you type *"Actually, how do I configure Docker?"*, Jugnu instantly discards the old context, dynamically builds a new query, and re-runs semantic search to prevent hallucination.
- **AI Engine (Ollama)**: Automatically connects to a local `gemma4:e2b` model. Employs a full-sized `num_ctx: 2048` `_warmup()` routine to safely absorb CUDA KV-Cache reallocation crashes on RTX 4050 mobile GPUs before real queries execute.
- **Concurrent Architecture**: SQLite is configured in WAL (Write-Ahead Logging) mode, allowing the C++ daemon to flush thousands of telemetry rows instantly while Python inference concurrently reads vectors without `SQLITE_BUSY` contention.

---

## 📚 The Open Knowledge Format (OKF)

Jugnu leverages a hyper-structured schema inspired by Google's [Open Knowledge Format (OKF)](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing). Instead of dumping raw, noisy OCR pixels into a vector database, Jugnu executes a **Two-Pass Synthesis Pipeline**:

1. **Pass 1 (Extractor)**: Gemma strictly slices out UI scrollbars, ads, and boilerplate, returning only factual semantic bullet points.
2. **Pass 2 (Synthesizer)**: Gemma structures the clean bullets into a deterministic plain-text schema (`TOPIC:`, `TAGS:`, `CONTENT:`). This completely sidesteps LLM string-escaping bugs (like unescaped C++ newlines crashing `json.loads`) while maintaining perfect programmatic extractability.

These structured knowledge blocks are then indexed by `sqlite-vec` into a dual-table `knowledge_docs` and `vec_knowledge` structure. This ensures that when Jugnu searches for context, it retrieves clean, objective knowledge rather than a chaotic jumble of text.

---

## 📅 Development Roadmap (Future Plans)

### Phase 4 — Open Knowledge Format & Lazy RAG (Completed)
- [x] Complete Two-Pass OKF synthesis for pristine vector embeddings.
- [x] Implement Lazy RAG to defer massive GPU workloads until user confirmation.
- [x] Implement Battery-aware background task deferral (Settle times + `difflib` caching).

### Phase 5 — UIA Fast-Path & IPC Hardening (Completed)
- [x] Integrate COM `IUIAutomation` for O(1) text extraction without GPU OCR.
- [x] Migrate Named Pipe listener to Asynchronous Overlapped I/O to eliminate kernel deadlocks.
- [x] Harden LLM Pipeline against JSON Markdown hallucination traps.

### Phase 6 — Native UI & Cloud Escalation
- [ ] Implement Gemini API logic to act as a fallback when the local 4B model lacks confidence.
- [ ] Port the PowerShell terminal UI to a fully native C++ WebView2 borderless window experience.

---

## 🖥️ Hardware Requirements

| Component | Minimum | Recommended Setup |
|---|---|---|
| OS | Windows 10 v1903+ | Windows 11 |
| CPU | Any modern Intel/AMD | Intel i5 13th Gen+ |
| GPU | CPU fallback supported | RTX 4050 6GB (full CUDA offload for `e2b` quantization) |
| RAM | 8GB+ | 16GB LPDDR5X |

---

## 🧩 Tech Stack
- **C++20 (MSVC)**: Win32 API, WinRT, `Windows.Media.Ocr`, `ReadDirectoryChangesW`
- **Python 3**: `uv` package management, `pywin32`, PowerShell Terminal UI
- **AI/ML**: `ollama` (Gemma4:e2b), `sentence-transformers` (multilingual-e5)
- **Database**: `sqlite3` bundled with `sqlite-vec` extension for unified memory

---
*Built with ❤️ for rapid learning, offline privacy, and hyper-optimized OS telemetry.*
