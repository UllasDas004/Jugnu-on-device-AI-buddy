# 👁️ Jugnu — Screen Monitoring Architecture

Jugnu must understand what is happening on the screen to build its memory. Because it runs on Windows, it uses a highly efficient, multi-tiered approach rather than relying on raw screenshots.

---

## 1. The Trigger: App Switching
Jugnu does not poll the screen constantly. Instead, the C++ engine registers a Windows hook (`SetWinEventHook`) for `EVENT_SYSTEM_FOREGROUND`. 

Whenever the active window changes (e.g. you Alt+Tab from Chrome to VS Code), Windows instantly notifies Jugnu. 
1. Jugnu updates the Markov Chain predicting your next app.
2. Jugnu updates the Exponential Moving Average (EMA) importance score of the app.
3. Jugnu decides whether to read the screen based on the App Blocklist.

---

## 2. The 3-Tier Screen Reading Pipeline

If an app is deemed "safe to read" (not incognito, not a game, not playing video), Jugnu attempts to extract text using a cascading 3-tier system. It starts with the cheapest method, and only falls back to heavier methods if necessary.

### Tier 1: UI Automation (Near 0% CPU)
**Technology:** Microsoft `IUIAutomation` COM API.
This is the same API used by Windows Narrator. It directly asks the active application (like VS Code or Chrome) to hand over its text tree. It uses a **Breadth-First Search (BFS)** traversal with an `unordered_set` (O(1)) to merge and deduplicate nested UI nodes, guaranteeing pristine text blocks without the overlapping fragment explosion found in naive recursive DOM reads.
- **Pros:** Instant, flawless accuracy, practically zero CPU overhead. Directly isolates raw code or markdown.
- **Cons:** Some apps (like Discord or specific game UIs) do not expose their text trees properly to UI Automation.

### Tier 2: WGC + OCR (Hardware Accelerated via Native C++)
**Technology:** Windows Graphics Capture (WGC) + `Windows.Media.Ocr` (MSVC C++/WinRT).
If Tier 1 fails to find meaningful text, Jugnu takes a high-speed, invisible capture of the window into RAM using WGC, and processes it directly on the GPU using the native Windows OCR engine.
- **Pros:** Works on literally anything, including images, PDFs, and custom UIs. No Python subprocesses required (avoiding the CPU spikes seen in earlier versions).
- **Cons:** Slightly heavier on the GPU than Tier 1, but massively more efficient than Python `mss` or `tesseract`.

### Tier 3: The Ignore List
If Jugnu detects massive screen updates with no readable text (e.g., a video game rendering via DirectX, or a full-screen YouTube video), it automatically categorizes the app into Tier 3.
- **Action:** Halts all text extraction for this app to save battery, but continues to log *time spent* in the app to update the EMA priority map.

---

## 3. The Embedding Gate (Two-Stage Pipeline)

Once text is successfully extracted via Tier 1 or Tier 2, it isn't automatically saved as a permanent memory.

**Stage 1: The Buffer**
1. C++ dumps the raw, noisy text directly into the SQLite `ocr_buffer` table. This happens instantly, with zero python overhead.

**Stage 2: The Flush Worker**
1. A background Python daemon (`flush_worker.py`) wakes up every 60 seconds (but only if the laptop is plugged into AC power, using Win32 `GetSystemPowerStatus`, to save battery).
2. It waits for the UI to settle by enforcing a hard 30-second `Settle Time` (if the latest screenshot is less than 30s old, it aborts the cycle).
3. It reads chunks from the `ocr_buffer`.
4. It performs a fast `difflib.SequenceMatcher` check against the previously processed screen. If the screen hasn't changed by at least 15%, it instantly discards the chunk to save GPU battery. The cache for this is strictly capped (`MAX_CACHE = 20`) to prevent OOM memory leaks.
5. **The Pipeline Split**:
   - **UIA Fast-Path:** If the payload contains the `===SECTION===` delimiter (originating from Tier 1 UIA), Python **skips AI Extraction completely**. UIA text is already perfectly clean. It gets routed instantly to the final OKF Synthesizer (Step 6), achieving a 500% speedup.
   - **OCR Extraction (Fallback):** If the text is messy (Tier 2 OCR), it chunks the text into 500-character windows and uses Gemma to extract pure technical knowledge via strict text-parsing.
6. The clean text is synthesized into a structured JSON `knowledge_doc` (bypassing JSON Markdown Hallucinations).
7. The OKF document is embedded (384-dimensional vector) and saved to the permanent `knowledge_docs` table for RAG.
8. The processed rows are deleted from the buffer.

*(Note: See `memory_system.md` for database schema details and `prompts_design.md` for the OKF prompts).*
