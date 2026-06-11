# Engineering Traps & Fixes — Jugnu

> Every trap listed here was either:
> - **Discovered in Synapse** (Android — ported and pre-fixed for Windows), OR
> - **Identified during Windows architecture design** (Windows-specific new traps)
>
> Format: What goes wrong → Why non-obvious → Concrete fix → Source

---

## Group A: Ported from Synapse — Data Layer

### Trap A-1: SQLite Thread Corruption (FULLMUTEX)
**Problem:** Background flush thread writes to SQLite while main thread queries. Corrupted DB, silent crash.  
**Non-obvious:** Only manifests under memory pressure, never in clean dev runs.  
**Fix:** Open with `SQLITE_OPEN_FULLMUTEX`. Wrap all SQL in `std::mutex` lock.  
**Source:** Synapse Trap B-1

---

### Trap A-2: Screen/Content Duplication in episodic_log
**Problem:** Same text seen twice (app switch back, clipboard paste of old content). Stored as duplicate rows. RAG diluted.  
**Fix:** Three-layer dedup:
1. `std::unordered_set` in RAM (session-level O(1) reject)
2. `checkTextExists()` SQL check (cross-session)
3. `UNIQUE` constraint on `core_persona.fact_text`  
**Source:** Synapse Trap B-2

---

### Trap A-3: 5,000-Row Limit With No Enforcement
**Problem:** Design specifies cap. No code enforces it. DB grows forever.  
**Fix:**
```sql
-- O(1) math deletion — NOT the O(N) NOT IN version
DELETE FROM episodic_log WHERE id <= (SELECT MAX(id) - 5000 FROM episodic_log);
```
Called in nightly job. Not on every flush (saves battery).  
**Source:** Synapse Traps B-3 and G-1

---

## Group B: Ported from Synapse — Memory & Priority

### Trap B-1: EMA Float Underflow (Subnormal Floats)
**Problem:** `P_old * 0.8` repeated on idle apps → subnormal float territory → 100-1000x slower ARM arithmetic.  
**Fix:**
```cpp
if (priority_map[app] < 0.1f) priority_map.erase(app);
```
**Source:** Synapse Trap C-1

---

### Trap B-2: EMA Eviction Deleting Permanent Facts
**Problem:** Google Keep has low EMA (rarely used). LRU evicts its entries. Entry: "User is allergic to peanuts." Deleted silently.  
**Non-obvious:** App-level priority and data-level importance are completely orthogonal.  
**Fix:** EMA-driven eviction ONLY touches `episodic_log`. `core_persona` is NEVER touched by any eviction or decay logic. Enforced by table design.  
**Source:** Synapse Trap C-2

---

## Group C: Ported from Synapse — Identity Confusion

### Trap C-1: Third-Party Text Poisoning the Persona
**Problem:** User's friend types "I have diabetes" in Discord DM. Our monitor reads the screen. LLM extracts it as a fact about the user.  
**Fix:** Authorship tagging. Only `is_authored = TRUE` rows go to nightly LLM extraction. Windows authorship detection uses:
1. UI Automation: focused element is `UIA_EditControlTypeId`
2. Source is `TextSource::CLIPBOARD` (user explicitly copied)
3. Source is `TextSource::FILE_WRITE` (file system write event)  
**Source:** Synapse Trap D-1

---

## Group D: Ported from Synapse — Persistence / Crash

### Trap D-1: Process Kill Wipes All Learned Priorities
**Problem:** Windows: SIGKILL, power button, crash. `unordered_map` in RAM → gone. Weeks of learned EMA/Markov → lost.  
**Fix:** 30-min background thread flushes both maps to SQLite. On boot, `loadPriorityMap()` and `loadTransitionMatrix()` restore full state. Max loss = 30 minutes.  
**Source:** Synapse Trap E-1

---

### Trap D-2: Flush-Before-Read Race Condition
**Problem:** Nightly job reads episodic_log → extracts facts → then flushes. If crash happens between read and flush, in-RAM rows buffered since last flush are LOST forever. Extraction never saw them, wipe already ran.  
**Fix:** `flushToDisk()` is Step 1 of nightly job. All RAM rows hit disk before extraction reads anything.  
**Source:** Synapse Trap E-2

---

### Trap D-3: Mid-Job Rows Wiped by Unconditional DELETE
**Problem:** Nightly job runs at 2AM, takes 3 min. New rows arrive during those 3 min. Unconditional DELETE wipes them.  
**Fix:** Rolling window extraction. Only process rows with `timestamp > LAST_NIGHTLY_RUN`. After extraction, call `enforceRowLimit()` which trims lowest-priority rows — never the fresh mid-job ones.  
**Source:** Synapse Trap E-3

---

## Group E: Ported from Synapse — ML Pipeline

### Trap E-1: Vague Extraction Prompt → LLM Hallucination
**Problem:** LLM invents facts, merges statements, returns prose. Parser breaks. Garbage in `core_persona`.  
**Fix:** Structured `FACT:` prefix output format. Explicit negative examples in prompt. Defensive parser: only accept `FACT:`-prefixed lines, reject < 5 chars, reject > 300 chars.  
**Source:** Synapse Trap F-1

---

### Trap E-2: Shallow Dedup Pollutes core_persona
**Problem:** Two extraction chunks produce:
- "User is allergic to peanuts"
- "User has a severe peanut allergy"
Both pass `distinct()`. Both stored. RAG returns 3 variations of same fact instead of 3 different facts.  
**Fix:** After string dedup, embed each candidate. `isFactAlreadyKnown(vector, 0.92f)` checks cosine similarity against existing `core_persona`. Similarity > 0.92 → skip.  
**Source:** Synapse Trap F-2

---

### Trap E-3: isFactAlreadyKnown() Crashes on Empty Table
**Problem:** On Day 1, `core_persona` has 0 rows. sqlite-vec vector search on empty table may throw. Nightly job crashes silently. No facts ever saved.  
**Fix:** C++ guard in `isFactAlreadyKnown()`:
```cpp
// Count rows first
int count = 0;
sqlite3_exec(db, "SELECT COUNT(*) FROM core_persona", countCallback, &count, nullptr);
if (count == 0) return false;  // Nothing to compare against — not known
```
**Source:** Synapse Trap F-3

---

### Trap E-4: Embedding a Package/Process Name
**Problem:** `predictNextApp()` returns "chrome.exe". Previous code tried `embed("chrome.exe")` and searched vectors. Process names have no semantic meaning in embedding space. Returns random context.  
**Fix:** Don't embed process names. Use SQL directly:
```sql
SELECT raw_text FROM episodic_log 
WHERE app_name = 'chrome.exe' 
ORDER BY timestamp DESC LIMIT 10
```  
**Source:** Synapse Trap F-4

---

### Trap E-5: sqlite-vec Macro Override (SQLITE_CORE)
**Problem:** Including `sqlite-vec.h` redefines all `sqlite3_*` functions as macros through a vtable pointer that only exists in extension code. Application code crashes with `'sqlite3_api' was not declared`.  
**Fix:**
```cpp
#define SQLITE_CORE          // MUST be before sqlite-vec.h include
#include "sqlite-vec.h"
```  
**Source:** Synapse Trap G-3

---

### Trap E-6: ONNX Runtime Not Thread-Safe
**Problem:** Preload thread + user query thread both call `e5-small` embedder simultaneously → undefined behavior → crash.  
**Fix:** Single `std::queue<EmbedRequest>` processed by one dedicated worker thread on its own `std::thread`. Preload requests are non-blocking (`try_push`, drop if full). User query requests are blocking (`push`, wait if full). Identical to Synapse's `embeddingChannel`.  
**Source:** Synapse Trap H-1

---

### Trap E-7: llama.cpp Not Thread-Safe
**Problem:** User sends a second message while first inference is running. Two `llama_decode()` calls on the same context → crash.  
**Fix:**
```cpp
std::mutex inferenceMutex;
// In generateAnswer():
std::lock_guard<std::mutex> lock(inferenceMutex);
// Only one call runs at a time
```  
**Source:** Synapse's `generatorMutex` equivalent for llama.cpp.

---

## Group F: Windows-Specific New Traps

### Trap F-1: SetWinEventHook Fires for Our Own Window
**Problem:** When Nexus's browser UI opens (or when we open any child window), `EVENT_SYSTEM_FOREGROUND` fires with our own process. We start tracking ourselves.  
**Fix:** `WINEVENT_SKIPOWNPROCESS` flag in `SetWinEventHook()`. One flag, zero additional code.

---

### Trap F-2: ReadDirectoryChangesW Floods on npm install
**Problem:** `npm install` creates thousands of files in `/node_modules/` in seconds. 10,000+ file change events per second. CPU spike, event buffer overflow, missed events.  
**Fix:**
```cpp
// Path-based filter applied before queuing
const std::vector<std::string> IGNORED_DIRS = {
    "node_modules", ".git", "dist", "build", "__pycache__", ".cache"
};
// + 2-second debounce on file change events (same as screen debounce)
```

---

### Trap F-3: Chrome Window Title Changes on Every Tab Switch
**Problem:** User has 20 tabs. Every tab click fires `EVENT_SYSTEM_FOREGROUND` with a new title. 20 WinEvent callbacks in 2 seconds. 20 UI Automation reads. CPU spike.  
**Fix:** 500ms debounce specific to `chrome.exe` and `msedge.exe`. Title must be stable for 500ms before we process it.

---

### Trap F-4: UI Automation COM Threading Model (STA)
**Problem:** `IUIAutomation` created on an MTA thread returns `CO_E_NOTINITIALIZED`. If we create it on a worker thread, every call fails silently.  
**Fix:** Dedicate one thread exclusively to UI Automation. Call `CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED)` on that thread. All `IUIAutomation` calls run on that thread only. Results are posted back to main via `std::queue`.

---

### Trap F-5: WGC Capture Item Use-After-Free
**Problem:** Window closes between `GraphicsCaptureItem` creation and `TryGetNextFrame()`. Accessing closed item → `0xC0000005` access violation crash.  
**Fix:** Wrap entire WGC capture in `try { } catch (winrt::hresult_error& e) { return ""; }`. Never assume the window is alive.

---

### Trap F-6: Windows OCR Returns Garbage on Visual Content
**Problem:** Window is a game, video player, or image viewer. `Windows.Media.Ocr` still runs and returns "lll", "0O0", random char artifacts. These pass the dedup filter and pollute episodic_log.  
**Fix:** Minimum real-word threshold. Count tokens with length > 3. If < 5 such words in OCR result → discard entire result. Garbage OCR always produces many 1-2 char artifacts.

---

### Trap F-7: UI Automation on Chrome Reads All Open Tabs
**Problem:** Chrome's accessibility tree exposes text from ALL tabs simultaneously, not just the visible viewport. We capture articles the user isn't reading.  
**Fix:** Viewport filter. For each UI element, call `get_CurrentBoundingRectangle()`. Only include elements whose bounding rect overlaps the visible window client area.

---

### Trap F-8: WGC SoftwareBitmap Stale After Session Close
**Problem:** `Direct3D11CaptureFrame.Surface()` becomes invalid after `session.Close()`. `SoftwareBitmap::CreateCopyFromSurfaceAsync()` called after close → blank image, no crash.  
**Non-obvious:** This is a lifetime issue, not a null pointer issue. The call succeeds but returns empty data.  
**Fix:** Strict order: Capture frame → `SoftwareBitmap::CreateCopyFromSurfaceAsync()` → **then** `session.Close()` → **then** OCR → discard bitmap.

---

### Trap F-9: YouTube Prevents Screen Stabilization (Continuous OCR Loop)
**Problem:** YouTube video changes visual content every frame. `WinEventHook` fires, 2s debounce starts, frame changes, debounce resets. Debounce never completes. We try Tier 1 OCR which also never stabilizes. CPU spins.  
**Fix:** Audio-aware debounce bypass:
```cpp
if (processName == "chrome.exe" && audioMonitor.isAudioPlaying("chrome.exe")) {
    // Browser is playing audio → video content → skip Tier 1 OCR entirely
    // Use only window title + Tier 0 UI Automation (which gets non-video text)
    return;
}
```

---

### Trap F-10: Process Name Collision with Python Engine
**Problem:** If any part of the system uses Python (scripts, model serving), `python.exe` appears in the process list. We accidentally track our own helper process.  
**Fix:** On startup, store our own PID and all child PIDs:
```cpp
DWORD ownPid = GetCurrentProcessId();
// In WinEventProc: filter out ownPid and any child of ownPid
```

---

### Trap F-11: Concurrent llama.cpp Inference + ONNX OOM
**Problem:** User sends a query (Gemma loads into VRAM, 2.7GB). Simultaneously, preload thread triggers e5-small embedding (ONNX, CPU). No individual OOM, but combined they can cause stutter on 6GB VRAM GPU under memory pressure.  
**Fix:** Inference scheduler. ONNX embedding waits for llama.cpp inference to complete before running. Both share a single inference state machine. No true parallelism on the ML layer.

---

## Calibration Items (Pre-Launch Validation Required)

| # | Item | Action |
|---|---|---|
| U-1 | `isFactAlreadyKnown()` on empty table | Test fresh DB, confirm returns `false`, not throw |
| U-2 | 0.92f cosine threshold | Run 20-30 paraphrase pairs through e5-small, validate threshold |
| U-3 | llama.cpp idle unload timer (5 min) | Confirm VRAM is actually freed after 5 min idle |
| U-4 | `parseFactsList()` at max token boundary | Simulate Gemma response cut off mid-`FACT:` line, confirm partial line rejected |
| U-5 | ReadDirectoryChangesW buffer overflow on npm install | Test with real `npm install`, confirm no missed events or crash |
| U-6 | Chrome viewport filter | Open Chrome with 10 tabs, confirm only visible tab content captured |

---

## Group G: Architecture Fallbacks & Contention Traps

### Trap G-1: The DRM / Black Screen Trap (WGC Fail)
**Problem:** Windows Graphics Capture (WGC) encounters a DRM-protected video (Netflix, Spotify, secure banking) or a hardware overlay. It returns a pure black rectangle. OCR processes it, wasting CPU, and returns garbage.
**Fix:** Bitmap Entropy Check. Before passing the WGC `SoftwareBitmap` to OCR, sample 20 random pixels. If 100% of sampled pixels are the exact same color (usually solid black `0x000000`), immediately abort OCR and fall back to purely logging the `window_title` and `process_name`.

### Trap G-2: VRAM Contention (AAA Gaming vs Gemma)
**Problem:** Gemma is loaded into the RTX 4050 (taking 2.7GB of 6GB). User launches a heavy game (Cyberpunk 2077) requiring 5GB VRAM. Windows panics, game stutters or crashes with `DXGI_ERROR_DEVICE_REMOVED`.
**Fix:** VRAM-Aware Auto-Unload. C++ monitors VRAM via WMI/DXGI. If usage crosses 85%, C++ instantly sends a `SIGINT`/Unload command over the Named Pipe to Python, purging Gemma to prioritize the foreground app.

### Trap G-3: The "Offline" Gemini API Trap
**Problem:** User queries a broad topic that routes to Gemini API. Laptop is disconnected from Wi-Fi. Python throws an unhandled network exception and crashes the inference service.
**Fix:** C++ checks the `INetworkListManager` *before* routing to Gemini. If offline, intercept the request and force-route to local Gemma with an injected system prompt: *"Note to model: You are offline and cannot search the web. Answer to the best of your local knowledge."*

### Trap G-4: UI Automation Thread Hang
**Problem:** User opens a massive Excel spreadsheet with 100,000 cells. `IUIAutomation` attempts to read the entire tree. The COM thread hangs for 5+ seconds, stalling the C++ engine.
**Fix:** Timeout-based worker queue. If `IUIAutomation` does not return the tree within 500ms, the worker thread abandons the COM call and the engine instantly falls back to Tier 2 (WGC+OCR) or logs the `window_title` only.



## UI Integration Traps & Deep Dives

### Trap: The PyWebView Event Loop Crash
- **Symptom**: `webview.start()` crashes immediately with `WebViewException: You must create a window first`.
- **Root Cause**: PyWebView acts as a wrapper around native web engines (Edge WebView2 on Windows, WebKit on macOS). These native engines require an initialized OS-level message loop to hook into. If no window handle (HWND) is spawned prior to calling `start()`, the message loop fails to bind.
- **The Code Fix**: 
  ```python
  # Spawns a hidden HWND to satisfy the WebView2 engine bindings
  webview.create_window("Jugnu Background Service", hidden=True)
  webview.start(pipe_listener_main, (state, engine), debug=False)
  ```

### Trap: IPC Pipe Deadlock via UI Blocking
- **Symptom**: Triggering the Stage 1 Notification Card causes the Python console to freeze. Any further C++ events (like window switching) are ignored indefinitely.
- **Root Cause**: The PyWebView UI requires the current thread to block and wait for user input (e.g. clicking the "Yes" button). Because our Named Pipe listener `win32file.ReadFile()` was running sequentially on the same thread, blocking the thread meant the pipe buffer filled up. Once the 4096-byte pipe buffer is full, the C++ `WriteFile` call blocks indefinitely too. The entire system deadlocks.
- **The Code Fix**:
  ```python
  # 1. Dispatch UI to a daemon thread
  threading.Thread(target=notification.trigger_flow, args=(state, engine), daemon=True).start()
  
  # 2. Inside the UI, block ONLY the daemon thread using an OS Event primitive
  done = threading.Event()
  win.events.closed += lambda: done.set()
  done.wait() # Sleeps the UI thread with 0% CPU cost until closed
  ```

### Trap: The Virtual Environment Feedback Loop
- **Symptom**: Running `uv add pywin32` freezes the C++ engine. The Python terminal floods with thousands of `FILE_SAVED` events for `uv.lock` and `.venv/Scripts`.
- **Root Cause**: The C++ `ReadDirectoryChangesW` watches the entire `D:\coding\Placements\projects\jugnu` path recursively. Package managers perform massive parallel disk I/O when installing dependencies. GhostWriter serialized every single modified file into JSON and shoved it through the Named Pipe.
- **The Code Fix**: 
  We must filter at the source in C++. By checking `filename.find(".venv") == std::string::npos`, we silently drop these events before they consume CPU cycles allocating JSON strings. We avoided `std::regex` because it's too slow for high-frequency kernel disk hooks.

### Trap: C-Extensions Missing in System Python
- **Symptom**: `ModuleNotFoundError: No module named 'win32file'` even after `pywin32` was successfully installed.
- **Root Cause**: Running `python inference/ipc_client.py` in PowerShell executes the global Python binary by default. The global Python has no knowledge of the isolated `.venv` directory created by `uv`.
- **The Code Fix**: Enforcing the use of `uv run python inference/ipc_client.py`. The `uv run` command intercepts the execution, modifies the `PATH` and `PYTHONPATH` environment variables dynamically, and points to the isolated C-Extension DLLs seamlessly.

---

## Group H: Phase 1.5 Live-Fire Traps (Discovered in Production)

### Trap H-1: JSON Backslash Corruption — Windows Paths in IPC Payloads
**Symptom:** Python's `json.loads()` crashes with `JSONDecodeError: Invalid escape`. The C++ `FILE_SAVED` event arrives with a payload like `{"file": "D:\inference\ai_engine.py"}`, and Python sees `\i` as an unknown JSON escape sequence.

**Root Cause:** Windows file paths use backslashes as separators (e.g. `D:\coding\jugnu`). However, the JSON specification mandates that a literal backslash **must** be encoded as a double-backslash `\\`. When C++ builds a raw JSON string by concatenating `watchPath + "\\" + filename`, the resulting single backslash is invalid JSON.

**The Non-Obvious Part:** This bug is invisible in local C++ unit tests because you're running on Windows and the C-style string `"\\"` represents a single backslash character in memory. The corruption only manifests at the Python `json.loads()` boundary because JSON is a language-agnostic text protocol with its own escape rules, distinct from C++ string literals.

**Fix:** In-place find-and-replace before serializing the path into the JSON payload:
```cpp
// In file_watcher.cpp, after building absolutePath:
std::string escapePath = absolutePath;  // e.g. D:\coding\jugnu\inference\ai_engine.py
size_t pos = 0;
while((pos = escapePath.find("\\", pos)) != std::string::npos)
{
    escapePath.replace(pos, 1, "\\\\");  // Replace single \ with \\\\
    pos += 2;  // CRITICAL: jump PAST the two chars just inserted, or we loop forever
}
std::string payload = "{\"type\": \"FILE_SAVED\", \"file\": \"" + escapePath + "\"}";
```
The `pos += 2` increment is the most dangerous line. If you write `pos += 1` instead, the loop finds the `\\` it just inserted, replaces it again with `\\\\`, and enters an infinite loop that consumes all RAM.

---

### Trap H-2: `ReadDirectoryChangesW` Returns Relative, Not Absolute Paths
**Symptom:** Python receives `{"file": "inference\\ai_engine.py"}` instead of the full path. `open(payload["file"])` raises `FileNotFoundError` because Python's CWD is different.

**Root Cause:** `ReadDirectoryChangesW` is designed as a *watcher*, not a path resolver. The `FILE_NOTIFY_INFORMATION.FileName` field only contains the path **relative** to the directory handle `hDir`. It never knows what absolute path was originally passed to `CreateFileA`.

**Fix:** Manually prepend `watchPath` before serializing:
```cpp
// FileName from the struct: "inference\ai_engine.py" (relative)
// watchPath stored on Start():  "D:\coding\Placements\projects\jugnu"
std::string absolutePath = watchPath + "\\" + filename;  // Absolute
// Then escape and serialize...
```
This is why `watchPath` is stored as a class-level static variable — so the background `WatcherThread` can access it when building the payload.

---

### Trap H-3: PyWebView COM Thread Violation Errors (Non-Fatal)
**Symptom:** On every notification trigger, the terminal floods with `System.InvalidCastException: Unable to cast COM object... CoreWebView2Controller members can only be accessed from the UI thread.`

**Root Cause:** This is a fundamental Windows COM threading model violation. COM (Component Object Model) is the underlying technology that WebView2 (and therefore pywebview) is built on. COM objects registered on the **Main UI Thread** (STA — Single-Threaded Apartment) cannot be called from a **background thread** (MTA — Multi-Threaded Apartment) without marshalling.

Our architecture deliberately offloads the notification UI to a `daemon=True` background thread to prevent deadlocking the Named Pipe reader. When that background thread calls `win.destroy()` on a window whose COM object was created on the Main Thread, Windows throws these exceptions.

**Why It Doesn't Crash:** `pywebview`'s internal C# layer wraps every COM call in a `try/catch`. When the exception fires, it logs it to stderr and then calls `Invoke()` to re-dispatch the destroy call back onto the correct UI thread. So the window closes correctly — the errors are informational spam, not fatal.

**Phase 4 Permanent Fix:** When we migrate to native C++ WebView2, all UI rendering will happen on the single C++ main thread. There will be no cross-thread COM violations because there's no Python daemon thread boundary.

---

### Trap H-4: `NoneType` Squiggle on `win.destroy()` — Python Type Narrowing
**Symptom:** Pylance (the VS Code Python type checker) draws red squiggles under `win.destroy()` and `win.events.closed` saying `Object of type NoneType has no attribute destroy`. The code runs fine, but the IDE constantly shows errors.

**Root Cause:** The type signature of `webview.create_window()` is `Window | None`. The function returns `None` if window creation fails (e.g. WebView2 not installed on the system). Pylance correctly interprets this: before checking for `None`, calling `.destroy()` on a potential `None` object is a type error.

**The Wrong Fix:** Adding a `type: ignore` comment. This hides the problem instead of solving it.

**The Correct Fix (Object-Oriented Injection Pattern):**
Instead of relying on a Python closure to capture `win` (which Pylance rightly flags), we inject the window reference into the API object **after** creation:
```python
class Api:
    def __init__(self):
        self.window: webview.Window | None = None  # Type-safe: explicitly allows None
    def yes(self):
        if self.window:          # Type guard: Pylance now knows self.window is Window
            self.window.destroy()

api = Api()
win = webview.create_window("Jugnu", html=NUDGE_HTML, js_api=api, ...)
if win:                   # Narrows type from 'Window | None' to 'Window'
    api.window = win      # Inject the confirmed-non-None Window into our Api
    win.events.closed += lambda: done.set()
```
This pattern is called **Dependency Injection**. The Api object doesn't know or care where its window comes from — we set it from outside. Pylance is now fully satisfied because the `if win:` guard narrows the type from `Window | None` to just `Window` before assignment.
