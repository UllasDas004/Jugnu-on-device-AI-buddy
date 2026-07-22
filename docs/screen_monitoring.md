# 👁️ Jugnu — Screen Monitoring Architecture

Jugnu must understand what is happening on the screen to build its memory. Because it runs on Windows, it uses a highly efficient, multi-tiered approach built for maximum performance and zero overhead when idle.

---

## 1. Zero-Overhead Hibernation & The Trigger

Jugnu operates as a background daemon but must strictly avoid stealing CPU cycles when the user is playing games or watching movies.

1. **The Event Hook:** The C++ engine registers a Windows hook (`SetWinEventHook`) for `EVENT_SYSTEM_FOREGROUND`. Windows instantly notifies Jugnu the exact millisecond the active window changes.
2. **Zero-Overhead Hibernation:** The background monitoring threads do not poll. When the user is outside a Deep Work app (e.g., in a game), the threads call `WaitForSingleObject(hDeepWorkEvent, INFINITE)`. The Windows Kernel parks them at absolute **0% CPU usage**. 
3. **Instant Wakeup & Dynamic Math:** When a whitelisted app (VS Code, Chrome) is focused, the hook calls `SetEvent()`. The threads wake up instantly. Even while active, they do not poll; they calculate exactly how much time is left until the 60-second idle threshold (`DWORD timeRemaining = 60000 - idleTime`) and sleep precisely for that duration.
4. **Anti-Idle Ghost Popup Trap:** Focus-stealing OS popups that occur while the user is physically away from the keyboard are ignored (`if(IsUserIdle()) { return; }`), preserving the true active context.
5. **The Ghost Clipboard Bypass:** The C++ engine also registers a native `AddClipboardFormatListener`. When the user explicitly copies/cuts code (CTRL+C), Jugnu intercepts it instantly, bypassing the 60-second timer entirely to guarantee a pristine, 100% complete file read flagged as `full_buffer=true`.

---

## 2. The 3-Tier Screen Reading Pipeline

When a Deep Work app remains idle for 60 seconds, Jugnu attempts to extract text using a cascading system. 

### Tier 1: UI Automation (Structured JSON)
**Technology:** Microsoft `IUIAutomation` COM API.
This directly asks the active application (like VS Code or Chrome) to hand over its text tree. It uses a **Depth-First Search (DFS)** traversal with aggressive **ARIA Pruning**. It explicitly checks `get_CurrentIsOffscreen()` to skip hidden noise, and prunes purely structural nodes (`Pane`, `Group`) unless they contain meaningful text, compressing the payload size by 90%.
- **The Output:** It extracts up to 5 of the longest, most meaningful sections on screen. It preserves parent-child relationships (a `Document` node absorbing its child `Text` nodes) but strictly isolates `Edit` nodes (code editors). It returns a structured JSON payload (`[{"type":"Edit", "name":"...", "text":"..."}, ...]`).
- **Pros:** Instant, flawless accuracy, almost zero CPU overhead. Isolates the code editor from the surrounding prose.

### Tier 2: WGC + OCR (Hardware Accelerated via Native C++)
**Technology:** Windows Graphics Capture (WGC) + `Windows.Media.Ocr` (MSVC C++/WinRT).
If Tier 1 fails to find meaningful text, Jugnu takes a high-speed, invisible capture of the window into RAM and processes it directly on the GPU using the native Windows 10/11 OCR engine.
- **Pros:** Works on literally anything, including images, PDFs, and unsupported UIs.
- **Cons:** Slightly heavier on the GPU than Tier 1. Produces a flat, unstructured text block.

### Tier 3: The Ignore List
If Jugnu detects massive screen updates with no readable text (e.g., full-screen video), it halts text extraction to save battery, but continues to log *time spent* to update the EMA priority map.

---

## 3. The OKF Data Cleaning Pipeline (Two-Stage)

Once text is successfully extracted, it goes through a robust cleaning and synthesis pipeline.

**Stage 1: The Zero-IPC Buffer**
1. C++ bypasses the Named Pipe IPC entirely to avoid serialization bloat. It writes the raw UTF-8 text directly into the SQLite `ocr_buffer` table. This happens instantly.

**Stage 2: The Python FlushWorker**
A background Python daemon (`flush_worker.py`) wakes up every 60 seconds to process the `ocr_buffer`.
1. **Power & Settle Gates:** It only runs on AC Power (checked via Win32 `GetSystemPowerStatus`). It enforces a hard 30-second `Settle Time`, refusing to process rows that were inserted less than 30 seconds ago to ensure the user is done typing.
2. **Staleness Purge:** Rows older than 10 minutes are deleted without processing.
3. **JSON Sanitization & Area-Wise Deduplication:** 
   - First, the incoming JSON is stripped of `\ufffc` (Object Replacement Character) and `\x00` bytes to prevent fatal token-limit stack overflows inside the LLM context.
   - Second, it performs an Area-Wise `difflib.SequenceMatcher` check against a FIFO cache (`MAX_CACHE=20`). Crucially, it decouples `Edit` controls (Code) from `Document` controls (Page Text). Both must independently be **>95% identical** to the last capture to skip processing. This guarantees that small code changes are captured even if the surrounding page text (like LeetCode problems) is huge and static.
4. **The Processing Split (Zero-Overhead Token Budget):**
   - **UIA Fast-Path:** The worker parses the JSON. `Edit` controls (user code) are treated as verbatim strings, bypassing Gemma completely. A heuristic filter drops UI noise like the Chrome URL bar. `Document` and `Text` controls are sent to Gemma.
   - **Gemma Token Optimization:** Gemma is only asked to generate `TOPIC`, `TAGS`, and `NOTES`. It no longer regurgitates `CONTENT`. The raw C++ UIA payload is piped directly through Python to the database, freeing up massive LLM output tokens and preventing generation cutoffs.
   - **OCR Fallback:** Noisy text is context-aware chunked into 500-character windows. Small noise chunks (<8 words) are discarded before Gemma extraction.
5. **Intelligent OKF Synthesis (The Embedder):** 
   - **Deterministic Anchors:** Before applying fuzzy vector math, the system checks for exact matches on `window_title` or `file_path`. If found, it bypasses vector similarity and merges directly, preventing identity duplication.
   - **Union Merging (Never Delete):** During `difflib` merging, the system enforces a "Never Delete, Only Add" policy. This ensures that if the user scrolls down in VSCode, the code that falls out of the viewport is not deleted from Jugnu's memory. Overwrites are only permitted when the `full_buffer` flag is true (Ghost Clipboard).
   - **OCR-to-UIA Upgrades:** If a new pristine UIA capture matches an old dirty OCR document, the system automatically overwrites the OCR text with the pixel-perfect UIA string.
6. **Safe Row Deletion:** 
   - Rows are marked for deletion from `ocr_buffer` *only after* synthesis succeeds. If Gemma crashes (OOM/Timeout), the row is retained in `ids_failed` and retried on the next cycle, guaranteeing zero data loss.
