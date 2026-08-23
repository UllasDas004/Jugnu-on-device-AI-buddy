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

## 2. The Hybrid Capture Engine (Gear 1 & Gear 2)

Previously, Jugnu used a static polling loop that fired a heavy UIA capture every time the user was idle for 60 seconds. This caused COM thread hangs on large files, generated massive SQLite DB spam, and often left the AI with stale context because the Python flush worker only ran every 60 seconds.

We replaced this with a highly optimized, context-aware dual-gear system:

### Gear 1: Tab/Window Switch (10s Debounce)
When the user switches to a new tab or window in a Focus App (like opening a new LeetCode problem), Jugnu waits for exactly 10 seconds.
- **The Action:** If the user stays on the tab AND has been idle for 5+ seconds (not actively typing), Jugnu fires exactly ONE full UIA scan (walking the accessibility tree + optional Ghost Clipboard for code editors). This is now written **directly to `knowledge_docs`**, bypassing `ocr_buffer` entirely.
- **Why it's better:** Direct write avoids the 60-second FlushWorker delay. C++ immediately sends an `UIA_EXTRACTION_SAVED` IPC event with the `row_id` so Python enriches it (TOPIC, TAGS, NOTES, KNN embedding) in a background thread without blocking the pipe reader.

### Gear 2: Active Typing Hot-Path (5s Pause)
While the user is actively coding, a background check fires when the user pauses typing for more than 5 seconds (`GetLastInputInfo` polling).
- **The Action:** Jugnu fires a targeted Ghost Clipboard extraction (synthetic Ctrl+A + Ctrl+C on the Monaco editor). Crucially, this bypasses the UIA COM tree entirely, and **instead of writing to SQLite**, saves the perfectly escaped code directly into a volatile `g_lastCodeBuffer` in RAM.
- **Why it's better:**
  1. **Zero Disk I/O:** No SQLite DB spam, no Python wakeups, no disk writes while the user is in a flow state.
  2. **Perfect Code Accuracy:** Ghost Clipboard explicitly copies the Monaco/VSCode editor buffer, avoiding UIA truncation or off-screen scroll issues.
  3. **0ms Staleness:** When the 3-minute Stuck Timer fires, it injects this hot RAM cache directly into the IPC payload (`"code": "..."` field). Gemma sees the exact code on screen instantly, completely bypassing the Python DB read pipeline.

### Tier 2: WGC + OCR (Hardware Accelerated via Native C++)
**Technology:** Windows Graphics Capture (WGC) + `Windows.Media.Ocr` (MSVC C++/WinRT).
If UIA fails to find meaningful text, Jugnu takes a high-speed, invisible capture of the window into RAM and processes it directly on the GPU using the native Windows 10/11 OCR engine.
- **Pros:** Works on literally anything, including images, PDFs, and unsupported UIs.
- **Cons:** Slightly heavier on the GPU than Tier 1. Produces a flat, unstructured text block.

### Tier 3: The Ignore List
If Jugnu detects massive screen updates with no readable text (e.g., full-screen video), it halts text extraction to save battery, but continues to log *time spent* to update the EMA priority map.

---

## 3. The OKF Data Cleaning Pipeline (Two-Stage)

Once text is successfully extracted, it goes through a robust cleaning and synthesis pipeline.

**Stage 1: Direct-to-`knowledge_docs` (Gear 1 Fast Path)**
Since Phase 8, Gear 1 tab-switch UIA captures are written **directly to `knowledge_docs`** by the C++ `DBHandler::SaveToKnowledgeDocs()` call. `ocr_buffer` is **not used** for this path. The C++ engine then immediately sends a `UIA_EXTRACTION_SAVED` IPC event to Python, which triggers `flush_worker.process_uia_by_id(row_id)` in a background thread to add Gemma-generated metadata and KNN embeddings.

**Stage 1b: ocr_buffer Fallback (Background OCR Only)**
The `ocr_buffer` staging table is now used exclusively for the WinRT OCR fallback path (when UIA finds no text). C++ writes raw OCR blobs here; Python's FlushWorker processes them on the 60-second cycle.

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

---

## 4. Phase 8 — ScreenReader Enhancements

### 4.1 UIA Direct-to-`knowledge_docs` Fast Path
Previously, all C++ captures went into `ocr_buffer` for Python's FlushWorker to process. In Phase 8, Gear 1 tab-switch UIA captures are written **directly** to `knowledge_docs` via `DBHandler::SaveToKnowledgeDocs()`, bypassing `ocr_buffer` entirely.

**Why:** `knowledge_docs` is the authoritative structured store. Writing directly avoids one full FlushWorker cycle (60s delay) for the problem context the CP mode needs immediately.

After the direct write, C++ sends `UIA_EXTRACTION_SAVED` IPC event with the `row_id`. Python's `flush_worker.process_uia_by_id(row_id)` then immediately enriches the row with Gemma-generated metadata (TOPIC, TAGS, NOTES) and embeds it into `vec_knowledge`. This happens in a background thread — the IPC pipe is never blocked.

### 4.2 RootWebArea URL Extraction
Previous versions could only extract page text. Phase 8 adds reliable URL extraction directly from the UIA accessibility tree, without requiring the Chrome address bar to be focused.

**Method:** After DFS, the first `UIA_DocumentControlTypeId` element with a non-empty `Name` property is the `RootWebArea`. Its URL is retrieved via `LegacyIAccessiblePattern::get_CurrentValue()`. Chrome always populates this regardless of focus state.

**Stored as:** A `{"type":"PageMeta","title":"...","url":"..."}` JSON object at the front of the UIA result array. Python's `flush_worker` splits this out and saves `source_url` to `knowledge_docs`, enabling accurate deduplication by URL instead of just window title.

### 4.3 InputHooks — Synthetic Input Filtering
The CP-mode `InputHooks` (`WH_KEYBOARD_LL` + `WH_MOUSE_LL`) now explicitly filter synthetic inputs from Ghost Clipboard operations using the `LLKHF_INJECTED` and `LLMHF_INJECTED` flags. This prevents `g_cpKeyStrokeCount` from incrementing during Ctrl+A/Ctrl+C extraction, keeping the CP state machine's idle timers accurate.

### 4.4 BoundedSimilarityRatio — O(N) Levenshtein Guard
A custom C++ bounded Levenshtein implementation with early abort at 2% diff threshold. Used to detect near-identical UIA captures (e.g., user scrolled slightly) without running a full O(N²) comparison:

```cpp
static double BoundedSimilarityRatio(const std::string& s1, const std::string& s2)
// Aborts early if abs(len1 - len2) > 2% * max(len1, len2)
// Returns 0.0 (definitely different) or similarity ratio 0.0-1.0
```

### 4.5 CP Abandon Detection (Rage-Quit Catcher)
`ScreenReader::ReaderThread` now tracks `lastProblemTitle`. On any tab change where the previous title was a CP problem, it sends a `PRACTICE_ABANDONED` IPC event to Python with the title and last known code snapshot. This allows the Practice Mode to mark the session as abandoned and update stats.

### 4.6 Platform Auto-Detection in Session Start
When a new tab settles after 10 seconds:
1. Window title is lowercased and checked for `"leetcode"` or `"codeforces"`
2. Platform string is derived automatically (`"leetcode"` / `"codeforces"`)
3. Slug is derived: `title.substr(0, title.find(" - "))` → spaces replaced with `-`
4. `CPStateManager::StartSession(slug, platform)` is called immediately

No Python-side parsing needed — C++ sends the clean slug and platform in the `CP_SESSION_START` IPC payload.
