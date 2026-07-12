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

### Trap E-5: sqlite-vec Header Inclusion Macro Override
**Problem:** Including `sqlite-vec.h` pulls in `sqlite3ext.h`, which redefines ALL
`sqlite3_*` functions as macro pointers (`sqlite3_api->exec`). This pointer only exists
in dynamically loaded `.dll` extension code. In a statically linked application:
```
error: 'sqlite3_api' was not declared in this scope
```
This error appears on **every** sqlite3 call in the translation unit, not just the vec call.

**The Wrong Fix:** `#define SQLITE_CORE` before the include. Only works when building the
extension source itself, not in application code.

**The Correct Fix:** Never include `sqlite-vec.h` in application code. Forward-declare only:
```cpp
// db_handler.cpp — before namespace
extern "C" {
    int sqlite3_vec_init(sqlite3* db, char** pzErrMsg, const void* pApi);
}
// Inside DBHandler::Init(), BEFORE sqlite3_open_v2:
sqlite3_auto_extension((void(*)(void))sqlite3_vec_init);
```
**Source:** Discovered live in Phase 2 build

---

### Trap E-8: Python SQLite SQLITE_BUSY on Concurrent C++ Transaction
**Problem:** Python's `Embedder.save_memory()` INSERTs a vector while C++ flush thread
is inside a `BEGIN TRANSACTION`. Python gets `sqlite3.OperationalError: database is locked`.

**Non-obvious:** Only manifests every 30 minutes (when flush fires). Invisible in dev testing.

**Fix:**
```python
conn = sqlite3.connect("jugnu.db", timeout=5.0, check_same_thread=False)
```
SQLite retries internally for 5 seconds. C++ transactions are sub-millisecond, so Python
waits < 1ms in practice.
**Source:** Phase 2 Architecture Decision

---

### Trap E-9: e5-small Asymmetric Prefix Mismatch
**Problem:** Storing and querying text without instruction prefixes produces low-quality
cosine similarity. The search returns irrelevant memories. No crash — the bug is silent
data quality corruption.

**Root Cause:** e5-small-v2 is an *asymmetric* model fine-tuned with different prefixes
for passages (stored) vs queries (searched). Without them, vectors land in the wrong
region of embedding space and similarity scores become noisy.

**Fix:**
```python
# Storing text:
embedding = model.encode(f"passage: {text}", normalize_embeddings=True)

# Searching:
query_vec = model.encode(f"query: {query_text}", normalize_embeddings=True)
```
**Source:** Phase 2, intfloat/e5-small-v2 documentation

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

### Trap F-12: The Python OCR Subprocess Battery Drain
**Problem:** Originally, OCR was implemented by spawning a Python `subprocess.Popen` to capture the screen and run Tesseract/PowerShell. This approach bypassed the C++ engine entirely, resulting in 100% CPU spikes, memory bloat, and severe battery drain.
**Fix:** Complete architectural compiler migration from MinGW/GCC to **MSVC (Visual Studio Build Tools)**. This unlocked native Windows Runtime (WinRT) APIs in C++. We replaced the Python subprocess with native `Windows.Media.Ocr`, which captures frames directly to RAM via WGC and executes purely on the GPU.

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

### Trap H-3: PyWebView COM Thread Violation + pythonnet Recursion Crash
**Symptom:** On every notification trigger, the terminal floods with `SyncRoot.SyncRoot.SyncRoot: maximum recursion depth exceeded` and the Python service crashes or hangs permanently.

**Root Cause:** Two cascading bugs:
1. **COM Threading:** Closing a window (`win.destroy()`) from a background thread violates Windows Single-Threaded Apartment (STA) rules. PyWebView catches this `InvalidCastException` internally and passes it to the Python `logging` module.
2. **pythonnet Recursion (The Fatal Part):** When `logging` tries to stringify the COM exception, the `.NET` exception wrapper (`pythonnet`) inspects the object properties. It hits the `SyncRoot` property, which points to itself. Python recursively inspects `SyncRoot` until it hits the recursion limit and crashes the whole thread.

**The Fix:** We cannot easily avoid cross-thread calls without a massive UI architectural rewrite. Since PyWebView safely re-dispatches the `destroy()` to the main thread internally *after* logging the error, we just need to prevent the `logging` module from ever seeing the exception:
```python
# Mute PyWebView so it stops feeding .NET objects to pythonnet's buggy logger
import logging
logging.getLogger('pywebview').setLevel(logging.CRITICAL)
```
**Source:** Phase 1.5 Live-Fire, pythonnet issue #1126.

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

---

### Trap H-5: Ollama CUDA Stack Overrun (0xc0000409) on Warmup
**Symptom:** Ollama server completely crashes with `exit status 0xc0000409: The system detected an overrun of a stack-based buffer` when evaluating the first prompt on `gemma4:e2b`.
**Root Cause:** A known driver/CUDA mismatch issue specifically on mobile GPUs (like RTX 4050 Laptop) when `ggml_cuda_kernel_can_use_pdl` attempts to initialize the `cuda_v13` backend.
**Fix 1:** Force CPU fallback via `$env:OLLAMA_LLM_LIBRARY="cpu"` and restart `ollama serve`.
**Fix 2 (The Cold-Start Absorber):** Even on CPU, the first prompt can sometimes trigger a one-off initialization crash. In `ai_engine.py`, we emit a silent `_warmup()` query inside a try/except block at boot. This intentionally triggers and catches the crash gracefully, leaving the server warm and stable for real user queries.

---

### Trap H-6: Reasoning Models (gemma4:e2b) Returning Empty Strings
**Symptom:** The AI returns a successful 200 OK, but the Jugnu Insight window displays a completely blank message.
**Root Cause:** `gemma4:e2b` is a chain-of-thought reasoning model. It places its internal monologue inside a `<thought>` block. The Ollama Python library parses this into a separate `thinking` field, leaving the `content` field empty until the thought finishes. If `num_predict` is too low (e.g., 200 tokens), the model hits the limit while still thinking (`done_reason='length'`), resulting in a permanently empty `content` field.
**Fix:** 
1. Increase `num_predict` to 512 to give the model room to finish thinking.
2. In `ai_engine.py`, extract both fields. If `content` is empty, fallback to returning `[Thinking]...\n` + the `thinking` string, ensuring the UI never renders a blank box.

---

### Trap H-7: Python `KeyboardInterrupt` Swallowed by Win32 Pipe Exception
**Symptom:** When pressing `Ctrl+C` to gracefully shut down the Python `ipc_client.py` listener, the terminal dumps a massive traceback ending in `Normalization failed: type=error args=(109, 'ReadFile', 'The pipe has been ended.')`.
**Root Cause:** When the user hits `Ctrl+C`, the `KeyboardInterrupt` is thrown *while* Python is blocked inside the native C extension `win32file.ReadFile()`. The interrupt forces the C extension to abort the pipe read violently, returning the Win32 `109 ERROR_BROKEN_PIPE` code. This bubbles up concurrently with the `KeyboardInterrupt`, causing `pythonnet` and the traceback printer to collide.
**Fix:** Add an explicit `except KeyboardInterrupt:` block *below* the `pywintypes.error` block. This catches the abort signal, explicitly calls `win32file.CloseHandle(handle)` to release the kernel lock, and calls `sys.exit(0)` to shut down cleanly without traceback vomit.

---

### Trap H-8: The Ollama Pydantic vs Dict Breaking Change
**Symptom:** `response.get('message', {})` crashes with an `AttributeError: 'ChatResponse' object has no attribute 'get'`.
**Root Cause:** The newer `ollama` Python library migrated its return types from raw dictionaries (`dict`) to Pydantic objects (`ChatResponse`). Accessing fields via `.get()` fails.
**Fix:** Use an object-aware fallback block (`if hasattr(response, 'message')`) to safely extract `content` and `thinking` via standard attribute access, while keeping the `.get()` block as a fallback for older versions of the library. Also ensure `getattr()` is paired with `or ''` to handle `None` values gracefully.

---

### Trap H-9: The Stale Memory API Signature Crash
**Symptom:** The background `embedder.save_memory` thread silently crashes or saves corrupt paths when trying to embed clipboard data.
**Root Cause:** A refactor to the `save_memory` signature changed it to accept explicit positional arguments plus a `file_path` kwarg. Code calling the old `state.active_code_file` API format passed mismatched arguments, resulting in `None` being treated as a file path during semantic RAG vectoring.
**Fix:** Explicitly pass `kwargs={'file_path': None}` for clipboard/app telemetry, and `kwargs={'file_path': filepath}` for actual files in the IPC listener. In `state_manager.py`, if reading the physical `file_path` fails (e.g., deleted file or `None` clipboard path), safely fall back to using the raw `snippet` stored in the vector database directly, ensuring no crash occurs.

---

## Group I: Phase 3 OCR Dataflow Traps

### Trap I-1: The "Incomplete SQL Input" Trap
**Problem:** Defining a multi-line SQL query (like `CREATE TABLE ocr_buffer(...)`) in C++ using a raw string literal `R"()"` but missing the closing `);` inside the string. The C++ compiler ignores string contents, so it compiles perfectly, but SQLite crashes at runtime with `[DB] SQL error: incomplete input`.
**Fix:** Always test multi-line SQL queries in a native SQLite REPL before pasting them into C++ raw string literals.

### Trap I-2: The Offline Embedder IPC Tear-Down
**Problem:** The `SentenceTransformer` defaults to checking HuggingFace (`huggingface.co`) for updated config files on boot. If the user is offline (e.g. on a flight), the Python script crashes entirely with `[Errno 11001] getaddrinfo failed`. Because the Python process dies, the Named Pipe closes, and the C++ engine deadlocks trying to write to a broken pipe.
**Fix:** Implemented a custom `_is_online()` check using Python's `socket` library to ping port 80. If it fails, explicitly pass `local_files_only=True` to the `SentenceTransformer` constructor to bypass network calls and load from the `.cache`.

### Trap I-3: The Database Loop Closure Indentation
**Problem:** In `flush_worker.py`, the `conn.close()` statement was indented one tab too deep, placing it inside the `for chunk in chunks:` loop. The worker processed the first chunk successfully, closed the DB connection, and instantly crashed on the second chunk with `Cannot operate on a closed database`.
**Fix:** Strictly audit Python indentation when managing DB connections across batch loops.

### Trap I-4: The Stale C++ Engine Schema Mismatch
**Problem:** We updated `db_handler.cpp` to create the new `ocr_buffer` table, but forgot to recompile `jugnu.exe`. The Python script booted up and connected to the *old* running C++ executable, instantly crashing with `sqlite3.OperationalError: no such table: ocr_buffer`.
**Fix:** Maintain strict build discipline. Stop the C++ background daemon, run `ninja`, and restart `jugnu.exe` before testing Python scripts that rely on new C++ SQLite schemas.

### Trap I-5: Unprintable OCR Pixel Garbage
**Problem:** The WinRT OCR engine occasionally misinterprets graphical UI elements (like scrollbars) as ASCII control characters (like `0x1B` ESC). Passing these over the IPC pipe caused Python's `json.loads()` to throw a `JSONDecodeError: Invalid control character`.
**Fix:** The C++ IPC layer explicitly checks if `static_cast<unsigned char>(c) < 0x20` and converts raw bytes into safely escaped Unicode sequences (e.g., `\u001b`) before sending them.

---

## Group J: Phase 4 RAG & Battery Optimizations

### Trap J-1: The Blocking IPC Thread Trap
**Problem:** When a developer saves an 8,000-character code file, it takes several seconds for Gemma to synthesize it into an OKF document. Originally, this ran synchronously on the main IPC listener thread, causing Python to stop reading from the Named Pipe. The 4KB kernel buffer overflowed, dropping critical telemetry events from C++.
**Fix:** Refactored `ipc_client.py` to use `threading.Thread(target=_synthesize_and_save_file, daemon=True).start()`. This pushes heavy file synthesis into the background, allowing the IPC loop to return to `win32file.ReadFile` instantly.

### Trap J-2: The Idle-Time Battery Destroyer (Lazy Evaluation)
**Problem:** When the C++ engine detected the user was idle, Python would immediately run a massive RAG generation task (using 100% GPU for 5-10 seconds) to build an answer *before* popping up the notification window. If the user was just stretching their legs and didn't actually need help, those GPU cycles were entirely wasted, drastically hurting battery life.
**Fix:** Implemented Lazy RAG Evaluation. `ipc_client.py` now only generates a tiny 10-token `search_query` (<100ms) and performs the KNN vector search when idle. The heavy 500-token AI answer generation is completely deferred until the user explicitly clicks "Yes, I need help" on the notification UI.

### Trap J-3: The Disjointed UI Context Trap
**Problem:** Jugnu popped a notification based on a Python file currently open on the screen. The user clicked "Yes" but typed a manual question: *"Actually, how do I configure Docker?"* The LLM tried to answer the Docker question using the Python file as context, resulting in massive hallucinations.
**Fix:** Implemented the "Custom Problem RAG Override" in `notification.py`. If a custom problem is detected, it intercepts the workflow, discards the pre-fetched screen context, generates a fresh query for "Docker", runs a new semantic search, and answers using the new, highly relevant sources.

### Trap J-4: The OCR Settle-Time Battery Drain
**Problem:** The background `FlushWorker` woke up every 60 seconds to process the OCR screen dumps. If the user was staring at a static documentation page for 10 minutes, Jugnu re-ran the Gemma OKF Extraction on the *exact same pixels* 10 times, draining the laptop battery.
**Fix:** Added a two-stage defensive check in `flush_worker.py`:
1. **Time Settle Check**: If the latest screenshot timestamp is less than 30 seconds old, abort (wait for the screen to settle).
2. **Difflib Dedup**: Used `difflib.SequenceMatcher(None, current_text, previous_text).ratio()`. If the screen hasn't changed by at least 15%, bypass Gemma inference entirely.


### Trap K-1: The SQLite Exclusive Lock Freeze
**Problem:** The C++ FlushWorker dumped massive amounts of OCR data into SQLite. Because SQLite defaults to Rollback Journals, this locked the entire DB. The Python inference pipeline, trying to query context concurrently, threw SQLITE_BUSY crashes.
**Fix:** Executed PRAGMA journal_mode=WAL; and PRAGMA synchronous=NORMAL; upon initialization in db_handler.cpp to permit concurrent multi-threaded read/writes.

### Trap K-2: The TOCTOU Notification Race Condition
**Problem:** is_generating was a simple module-level boolean. If two OS idle events fired fractions of a second apart, both daemon threads read is_generating == False, entered the generation block, and spawned two simultaneous Gemma LLM calls, immediately crashing the GPU with an OOM error.
**Fix:** Replaced the boolean with a standard 	hreading.Lock(), utilizing _gen_lock.acquire(blocking=False) to guarantee atomic check-and-set semantics.

### Trap K-3: The Reverse Markov Prediction Sort
**Problem:** The core prediction logic GetPredictedNextApps used a.second < b.second, which sorted probabilities *ascending*. The AI prefetcher was explicitly pre-warming RAM with the apps the user was *least* likely to open!
**Fix:** Changed the comparator to > for descending sort to properly pre-fetch the most highly-weighted Markov edges.

### Trap K-4: The Win32 hPipe Data Race
**Problem:** The IPC listener thread reset hPipe = INVALID_HANDLE_VALUE upon client disconnect, while the main event thread read hPipe to stream JSON via WriteFile. With no mutex, the handle could be invalidated *during* a write, causing Undefined Behavior and silent memory corruption.
**Fix:** Introduced std::mutex pipeMutex and wrapped all read/write/reset operations of the handle in a std::lock_guard.

### Trap K-5: The Python Subprocess Antivirus Lock
**Problem:** When proc.kill() forcefully terminated the interactive PowerShell window, temp files (jugnu_state.json) were left on disk. Windows Defender immediately swooped in to scan the modified files, obtaining a kernel lock. Python's cleanup routine os.remove() threw a PermissionError because of the AV lock, crashing the entire notification loop.
**Fix:** Added a graceful proc.terminate() with timeout before kill(), and wrapped os.remove() in a 	ry...except OSError block to fail gracefully if the OS held a lock.

### Trap K-6: Unbounded Cache Dictionary Leaks
**Problem:** Dictionaries like _last_raw_by_app and _last_embedded grew infinitely as the user switched between hundreds of transient processes over weeks, causing a slow but steady RAM leak.
**Fix:** Implemented a max-size eviction strategy utilizing Python 3.7+ insertion-ordered dicts (self.cache.pop(next(iter(self.cache)))).

### Trap K-7: The Cooldown Math Math-Bug
**Problem:** Cooldown logic computed deadlines using 	ime.time() - (_COOLDOWN_YES - seconds). When the "Decline" timeout (900s) occurred after an "Accept" timeout (1200s), the math resulted in negative offsets, causing Jugnu to endlessly skip notifications.
**Fix:** Simplified the state machine to track an explicit absolute timestamp (_cooldown_until = time.time() + seconds).

### Trap K-8: The Thread Shutdown Hang (Ghost Threads)
**Problem:** `FileWatcher` and `ClipboardManager` threads were hanging on exit because they were blocked on synchronous OS calls (`ReadDirectoryChangesW` and `GetMessage`), preventing them from seeing the `isRunning = false` flag.
**Fix:** Explicitly wake the blocked threads using `CancelIoEx` and `PostThreadMessage(WM_QUIT)` during the shutdown sequence before waiting on the thread handles.

### Trap K-9: The Machine Learning Model Re-Initialization Grind
**Problem:** `OcrEngine::TryCreateFromLanguage` was being called inside the 2-second polling loop, forcing Windows to reload the heavy ML model from SSD into RAM on every frame, causing CPU spikes.
**Fix:** Shifted the initialization to a static global variable initialized exactly once in `Start()`, keeping the model hot in RAM.
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

### Trap H-3: PyWebView COM Thread Violation + pythonnet Recursion Crash
**Symptom:** On every notification trigger, the terminal floods with `SyncRoot.SyncRoot.SyncRoot: maximum recursion depth exceeded` and the Python service crashes or hangs permanently.

**Root Cause:** Two cascading bugs:
1. **COM Threading:** Closing a window (`win.destroy()`) from a background thread violates Windows Single-Threaded Apartment (STA) rules. PyWebView catches this `InvalidCastException` internally and passes it to the Python `logging` module.
2. **pythonnet Recursion (The Fatal Part):** When `logging` tries to stringify the COM exception, the `.NET` exception wrapper (`pythonnet`) inspects the object properties. It hits the `SyncRoot` property, which points to itself. Python recursively inspects `SyncRoot` until it hits the recursion limit and crashes the whole thread.

**The Fix:** We cannot easily avoid cross-thread calls without a massive UI architectural rewrite. Since PyWebView safely re-dispatches the `destroy()` to the main thread internally *after* logging the error, we just need to prevent the `logging` module from ever seeing the exception:
```python
# Mute PyWebView so it stops feeding .NET objects to pythonnet's buggy logger
import logging
logging.getLogger('pywebview').setLevel(logging.CRITICAL)
```
**Source:** Phase 1.5 Live-Fire, pythonnet issue #1126.

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

---

### Trap H-5: Ollama CUDA Stack Overrun (0xc0000409) on Warmup
**Symptom:** Ollama server completely crashes with `exit status 0xc0000409: The system detected an overrun of a stack-based buffer` when evaluating the first prompt on `gemma4:e2b`.
**Root Cause:** A known driver/CUDA mismatch issue specifically on mobile GPUs (like RTX 4050 Laptop) when `ggml_cuda_kernel_can_use_pdl` attempts to initialize the `cuda_v13` backend.
**Fix 1:** Force CPU fallback via `$env:OLLAMA_LLM_LIBRARY="cpu"` and restart `ollama serve`.
**Fix 2 (The Cold-Start Absorber):** Even on CPU, the first prompt can sometimes trigger a one-off initialization crash. In `ai_engine.py`, we emit a silent `_warmup()` query inside a try/except block at boot. This intentionally triggers and catches the crash gracefully, leaving the server warm and stable for real user queries.

---

### Trap H-6: Reasoning Models (gemma4:e2b) Returning Empty Strings
**Symptom:** The AI returns a successful 200 OK, but the Jugnu Insight window displays a completely blank message.
**Root Cause:** `gemma4:e2b` is a chain-of-thought reasoning model. It places its internal monologue inside a `<thought>` block. The Ollama Python library parses this into a separate `thinking` field, leaving the `content` field empty until the thought finishes. If `num_predict` is too low (e.g., 200 tokens), the model hits the limit while still thinking (`done_reason='length'`), resulting in a permanently empty `content` field.
**Fix:** 
1. Increase `num_predict` to 512 to give the model room to finish thinking.
2. In `ai_engine.py`, extract both fields. If `content` is empty, fallback to returning `[Thinking]...\n` + the `thinking` string, ensuring the UI never renders a blank box.

---

### Trap H-7: Python `KeyboardInterrupt` Swallowed by Win32 Pipe Exception
**Symptom:** When pressing `Ctrl+C` to gracefully shut down the Python `ipc_client.py` listener, the terminal dumps a massive traceback ending in `Normalization failed: type=error args=(109, 'ReadFile', 'The pipe has been ended.')`.
**Root Cause:** When the user hits `Ctrl+C`, the `KeyboardInterrupt` is thrown *while* Python is blocked inside the native C extension `win32file.ReadFile()`. The interrupt forces the C extension to abort the pipe read violently, returning the Win32 `109 ERROR_BROKEN_PIPE` code. This bubbles up concurrently with the `KeyboardInterrupt`, causing `pythonnet` and the traceback printer to collide.
**Fix:** Add an explicit `except KeyboardInterrupt:` block *below* the `pywintypes.error` block. This catches the abort signal, explicitly calls `win32file.CloseHandle(handle)` to release the kernel lock, and calls `sys.exit(0)` to shut down cleanly without traceback vomit.

---

### Trap H-8: The Ollama Pydantic vs Dict Breaking Change
**Symptom:** `response.get('message', {})` crashes with an `AttributeError: 'ChatResponse' object has no attribute 'get'`.
**Root Cause:** The newer `ollama` Python library migrated its return types from raw dictionaries (`dict`) to Pydantic objects (`ChatResponse`). Accessing fields via `.get()` fails.
**Fix:** Use an object-aware fallback block (`if hasattr(response, 'message')`) to safely extract `content` and `thinking` via standard attribute access, while keeping the `.get()` block as a fallback for older versions of the library. Also ensure `getattr()` is paired with `or ''` to handle `None` values gracefully.

---

### Trap H-9: The Stale Memory API Signature Crash
**Symptom:** The background `embedder.save_memory` thread silently crashes or saves corrupt paths when trying to embed clipboard data.
**Root Cause:** A refactor to the `save_memory` signature changed it to accept explicit positional arguments plus a `file_path` kwarg. Code calling the old `state.active_code_file` API format passed mismatched arguments, resulting in `None` being treated as a file path during semantic RAG vectoring.
**Fix:** Explicitly pass `kwargs={'file_path': None}` for clipboard/app telemetry, and `kwargs={'file_path': filepath}` for actual files in the IPC listener. In `state_manager.py`, if reading the physical `file_path` fails (e.g., deleted file or `None` clipboard path), safely fall back to using the raw `snippet` stored in the vector database directly, ensuring no crash occurs.

---

## Group I: Phase 3 OCR Dataflow Traps

### Trap I-1: The "Incomplete SQL Input" Trap
**Problem:** Defining a multi-line SQL query (like `CREATE TABLE ocr_buffer(...)`) in C++ using a raw string literal `R"()"` but missing the closing `);` inside the string. The C++ compiler ignores string contents, so it compiles perfectly, but SQLite crashes at runtime with `[DB] SQL error: incomplete input`.
**Fix:** Always test multi-line SQL queries in a native SQLite REPL before pasting them into C++ raw string literals.

### Trap I-2: The Offline Embedder IPC Tear-Down
**Problem:** The `SentenceTransformer` defaults to checking HuggingFace (`huggingface.co`) for updated config files on boot. If the user is offline (e.g. on a flight), the Python script crashes entirely with `[Errno 11001] getaddrinfo failed`. Because the Python process dies, the Named Pipe closes, and the C++ engine deadlocks trying to write to a broken pipe.
**Fix:** Implemented a custom `_is_online()` check using Python's `socket` library to ping port 80. If it fails, explicitly pass `local_files_only=True` to the `SentenceTransformer` constructor to bypass network calls and load from the `.cache`.

### Trap I-3: The Database Loop Closure Indentation
**Problem:** In `flush_worker.py`, the `conn.close()` statement was indented one tab too deep, placing it inside the `for chunk in chunks:` loop. The worker processed the first chunk successfully, closed the DB connection, and instantly crashed on the second chunk with `Cannot operate on a closed database`.
**Fix:** Strictly audit Python indentation when managing DB connections across batch loops.

### Trap I-4: The Stale C++ Engine Schema Mismatch
**Problem:** We updated `db_handler.cpp` to create the new `ocr_buffer` table, but forgot to recompile `jugnu.exe`. The Python script booted up and connected to the *old* running C++ executable, instantly crashing with `sqlite3.OperationalError: no such table: ocr_buffer`.
**Fix:** Maintain strict build discipline. Stop the C++ background daemon, run `ninja`, and restart `jugnu.exe` before testing Python scripts that rely on new C++ SQLite schemas.

### Trap I-5: Unprintable OCR Pixel Garbage
**Problem:** The WinRT OCR engine occasionally misinterprets graphical UI elements (like scrollbars) as ASCII control characters (like `0x1B` ESC). Passing these over the IPC pipe caused Python's `json.loads()` to throw a `JSONDecodeError: Invalid control character`.
**Fix:** The C++ IPC layer explicitly checks if `static_cast<unsigned char>(c) < 0x20` and converts raw bytes into safely escaped Unicode sequences (e.g., `\u001b`) before sending them.

---

## Group J: Phase 4 RAG & Battery Optimizations

### Trap J-1: The Blocking IPC Thread Trap
**Problem:** When a developer saves an 8,000-character code file, it takes several seconds for Gemma to synthesize it into an OKF document. Originally, this ran synchronously on the main IPC listener thread, causing Python to stop reading from the Named Pipe. The 4KB kernel buffer overflowed, dropping critical telemetry events from C++.
**Fix:** Refactored `ipc_client.py` to use `threading.Thread(target=_synthesize_and_save_file, daemon=True).start()`. This pushes heavy file synthesis into the background, allowing the IPC loop to return to `win32file.ReadFile` instantly.

### Trap J-2: The Idle-Time Battery Destroyer (Lazy Evaluation)
**Problem:** When the C++ engine detected the user was idle, Python would immediately run a massive RAG generation task (using 100% GPU for 5-10 seconds) to build an answer *before* popping up the notification window. If the user was just stretching their legs and didn't actually need help, those GPU cycles were entirely wasted, drastically hurting battery life.
**Fix:** Implemented Lazy RAG Evaluation. `ipc_client.py` now only generates a tiny 10-token `search_query` (<100ms) and performs the KNN vector search when idle. The heavy 500-token AI answer generation is completely deferred until the user explicitly clicks "Yes, I need help" on the notification UI.

### Trap J-3: The Disjointed UI Context Trap
**Problem:** Jugnu popped a notification based on a Python file currently open on the screen. The user clicked "Yes" but typed a manual question: *"Actually, how do I configure Docker?"* The LLM tried to answer the Docker question using the Python file as context, resulting in massive hallucinations.
**Fix:** Implemented the "Custom Problem RAG Override" in `notification.py`. If a custom problem is detected, it intercepts the workflow, discards the pre-fetched screen context, generates a fresh query for "Docker", runs a new semantic search, and answers using the new, highly relevant sources.

### Trap J-4: The OCR Settle-Time Battery Drain
**Problem:** The background `FlushWorker` woke up every 60 seconds to process the OCR screen dumps. If the user was staring at a static documentation page for 10 minutes, Jugnu re-ran the Gemma OKF Extraction on the *exact same pixels* 10 times, draining the laptop battery.
**Fix:** Added a two-stage defensive check in `flush_worker.py`:
1. **Time Settle Check**: If the latest screenshot timestamp is less than 30 seconds old, abort (wait for the screen to settle).
2. **Difflib Dedup**: Used `difflib.SequenceMatcher(None, current_text, previous_text).ratio()`. If the screen hasn't changed by at least 15%, bypass Gemma inference entirely.

---

## Group K: Synchronization & Data Races

### Trap K-1: The SQLite Exclusive Lock Freeze
**Problem:** The C++ FlushWorker dumped massive amounts of OCR data into SQLite. Because SQLite defaults to Rollback Journals, this locked the entire DB. The Python inference pipeline, trying to query context concurrently, threw SQLITE_BUSY crashes.
**Fix:** Executed PRAGMA journal_mode=WAL; and PRAGMA synchronous=NORMAL; upon initialization in db_handler.cpp to permit concurrent multi-threaded read/writes.

### Trap K-2: The TOCTOU Notification Race Condition
**Problem:** is_generating was a simple module-level boolean. If two OS idle events fired fractions of a second apart, both daemon threads read is_generating == False, entered the generation block, and spawned two simultaneous Gemma LLM calls, immediately crashing the GPU with an OOM error.
**Fix:** Replaced the boolean with a standard threading.Lock(), utilizing _gen_lock.acquire(blocking=False) to guarantee atomic check-and-set semantics.

### Trap K-3: The Reverse Markov Prediction Sort
**Problem:** The core prediction logic GetPredictedNextApps used a.second < b.second, which sorted probabilities *ascending*. The AI prefetcher was explicitly pre-warming RAM with the apps the user was *least* likely to open!
**Fix:** Changed the comparator to > for descending sort to properly pre-fetch the most highly-weighted Markov edges.

### Trap K-4: The Win32 hPipe Data Race
**Problem:** The IPC listener thread reset hPipe = INVALID_HANDLE_VALUE upon client disconnect, while the main event thread read hPipe to stream JSON via WriteFile. With no mutex, the handle could be invalidated *during* a write, causing Undefined Behavior and silent memory corruption.
**Fix:** Introduced std::mutex pipeMutex and wrapped all read/write/reset operations of the handle in a std::lock_guard.

### Trap K-5: The Python Subprocess Antivirus Lock
**Problem:** When proc.kill() forcefully terminated the interactive PowerShell window, temp files (jugnu_state.json) were left on disk. Windows Defender immediately swooped in to scan the modified files, obtaining a kernel lock. Python's cleanup routine os.remove() threw a PermissionError because of the AV lock, crashing the entire notification loop.
**Fix:** Added a graceful proc.terminate() with timeout before kill(), and wrapped os.remove() in a try...except OSError block to fail gracefully if the OS held a lock.

### Trap K-6: Unbounded Cache Dictionary Leaks
**Problem:** Dictionaries like _last_raw_by_app and _last_embedded grew infinitely as the user switched between hundreds of transient processes over weeks, causing a slow but steady RAM leak.
**Fix:** Implemented a max-size eviction strategy utilizing Python 3.7+ insertion-ordered dicts (self.cache.pop(next(iter(self.cache)))).

### Trap K-7: The Cooldown Math Math-Bug
**Problem:** Cooldown logic computed deadlines using time.time() - (_COOLDOWN_YES - seconds). When the "Decline" timeout (900s) occurred after an "Accept" timeout (1200s), the math resulted in negative offsets, causing Jugnu to endlessly skip notifications.
**Fix:** Simplified the state machine to track an explicit absolute timestamp (_cooldown_until = time.time() + seconds).

### Trap K-8: The Thread Shutdown Hang (Ghost Threads)
**Problem:** `FileWatcher` and `ClipboardManager` threads were hanging on exit because they were blocked on synchronous OS calls (`ReadDirectoryChangesW` and `GetMessage`), preventing them from seeing the `isRunning = false` flag.
**Fix:** Explicitly wake the blocked threads using `CancelIoEx` and `PostThreadMessage(WM_QUIT)` during the shutdown sequence before waiting on the thread handles.

### Trap K-9: The Machine Learning Model Re-Initialization Grind
**Problem:** `OcrEngine::TryCreateFromLanguage` was being called inside the 2-second polling loop, forcing Windows to reload the heavy ML model from SSD into RAM on every frame, causing CPU spikes.
**Fix:** Shifted the initialization to a static global variable initialized exactly once in `Start()`, keeping the model hot in RAM.

---

## Group L: Phase 5 UIA & Prompt Extraction Traps

### Trap L-1: The LLM Markdown Hallucination Trap
**Problem:** Gemma was strictly commanded via prompt injection to output raw JSON (`think=False`). However, the model has an intense pre-training bias towards Markdown formatting. It frequently wrapped its output in ` ```json ... ``` ` fences, causing `json.loads()` to throw a fatal `JSONDecodeError`.
**Fix:** Ceased fighting the model's architecture. Changed the extraction prompt to demand rigid plaintext headers (`TOPIC:`, `TAGS:`, `CODE:`). The Python parser now simply splits the string by newlines, guaranteeing a 100% extraction success rate free of styling hallucinations.

### Trap L-2: The WinRT vs COM Apartment Trap
**Problem:** To support hardware OCR via `Windows.Media.Ocr`, the MSVC toolchain requires modern C++/WinRT. However, standard COM initialization `CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED)` — which is required for `IUIAutomation` — began randomly failing with `RPC_E_CHANGED_MODE` because the underlying WinRT runtime expects a different initialization paradigm.
**Fix:** Replaced legacy COM initialization with `winrt::init_apartment(winrt::apartment_type::single_threaded)`, which correctly satisfies both the WinRT OCR engine and the legacy COM `IUIAutomation` interfaces simultaneously.

### Trap L-3: The UIA BFS Context Explosion Trap
**Problem:** Naive recursive traversal of the `IUIAutomation` DOM tree led to nested text duplication (e.g., reading a `div` element's `Name` property naturally includes its child `span`'s text; reading the child again duplicates the text). This massively bloated the IPC JSON payload and instantly exhausted the Gemma 4096 context window.
**Fix:** Implemented a Breadth-First Search (BFS) combined with a `std::unordered_set<std::wstring>` (O(1) lookup). By strictly deduplicating text fragments as they were discovered in the tree, we guaranteed a pristine, non-overlapping payload extraction.

### Trap K-10: JSON Quotes Injection
**Problem:** The string escape loop only escaped backslashes, ignoring double quotes. A file named `hack"ed.txt` injected a raw quote into the JSON payload, crashing the Python JSON parser.
**Fix:** Reused the robust `EscapeJSON` switch-statement implementation to handle all RFC-compliant JSON character escapes.

### Trap K-11: The 49-Day Uptime Crash (Integer Overflow)
**Problem:** `GetTickCount()` returns a 32-bit unsigned integer that wraps to 0 after 49.7 days. If the PC wasn't restarted, the idle time calculation would underflow, producing a massive false idle time.
**Fix:** Upgraded the API call to `GetTickCount64()`, which handles uptimes of millions of years without overflowing.

### Trap K-12: WinRT COM Threading Silently Crashing OCR
**Problem:** To prevent CPU spikes, the WinRT `OcrEngine` initialization was moved to `Start()` (main thread), while OCR execution happened in a spawned `ReaderThread`. Because WinRT objects are strictly tied to COM Thread Apartments, calling `.RecognizeAsync()` from the uninitialized worker thread threw `CO_E_NOTINITIALIZED`, silently crashing the thread instantly.
**Fix:** Explicitly called `winrt::init_apartment()` inside the `ReaderThread` immediately before instantiating the `OcrEngine` to ensure the background thread has its own active COM apartment.

### Trap K-13: The CUDA KV Cache Warmup Size Mismatch
**Problem:** The `_warmup()` call in `ai_engine.py` was explicitly designed to absorb the RTX 4050 Ollama CUDA crash (0xc0000409). However, it passed `num_ctx: 512`, whereas real RAG queries passed `num_ctx: 2048`. The mismatched size forced Ollama to reallocate the KV Cache on the GPU during the first real query, causing the server to crash in production instead of during warmup.
**Fix:** Matched the warmup call to `num_ctx: 2048` to guarantee the KV Cache is maximally allocated during the dummy query, safely absorbing the PDL crash.

### Trap K-14: The Multiline LLM JSON Parsing Crash
**Problem:** We instructed Gemma to output extracted technical knowledge strictly as a JSON object. However, when Gemma extracted complex LeetCode C++ snippets containing newlines and quotes, it failed to properly escape them (e.g., actual `\n` instead of `\\n`). Python's `json.loads()` immediately crashed with `JSONDecodeError: Unterminated string`.
**Fix:** Abandoned JSON-formatted LLM output entirely. Refactored the extraction prompt to use a strict raw-text schema (`TOPIC:`, `TAGS:`, `CONTENT:`). A robust Python loop now parses the headers and safely wraps the payload into a dictionary in-code, completely immune to LLM string-escaping failures.

---

## Group K (Continued): Round 6 — IPC Overlapped I/O Upgrade

### Trap K-15: The IPC Ghost Connection (Synchronous Pipe Recovery Failure)
**Problem:** When Python was killed with `Ctrl+C` while Jugnu's C++ engine was idle (no app switches happening), the original `PipeListnerThread` had no way to know Python had disconnected. The inner health-check loop (`PeekNamedPipe` every 100ms) could only detect a broken pipe if the C++ side actively attempted I/O. During idle periods with zero incoming events, the loop sat inside `Sleep(100)` without trying to write anything. The C++ engine remained stuck believing Python was still connected, holding the pipe handle open. When Python restarted and tried to open `\\.\pipe\jugnu_ipc`, the OS returned `ERROR_PIPE_BUSY` because the handle was still occupied by the ghost connection.
**Non-obvious:** The bug is impossible to reproduce during active sessions (when you are switching apps, WinMonitor fires events constantly, each `WriteFile` failing immediately). It only appears during idle sessions — the exact scenario the system was designed to handle.
**Fix:** First patched with `PeekNamedPipe` active polling (100ms loop). Then fully resolved by upgrading to Overlapped (Asynchronous) I/O — see Trap K-16.

### Trap K-16: The Polling Anti-Pattern in Health Checks (Overlapped I/O Upgrade)
**Problem:** The `PeekNamedPipe` fix for K-15 still made 10 kernel syscalls per second (864,000/day) purely to ask the kernel "is Python still alive?" This is a classic polling anti-pattern — it burns CPU cycles checking for state that could be delivered via an OS event instead.
**Non-obvious:** The performance impact is small in isolation, but this pattern at scale is what kills battery life on mobile hardware. The fix also revealed a secondary problem: because Python never writes data back through the Named Pipe (all AI outputs go directly to SQLite), a blocking `ReadFile` on the C++ side would never return even if Python crashed.
**Fix:** Upgraded `PipeListnerThread` to use Windows Overlapped (Asynchronous) I/O:
1. Added `FILE_FLAG_OVERLAPPED` to `CreateNamedPipeA` to enable async mode.
2. Created two Win32 Event objects (`hConnectEvent`, `hStopEvent`) via `CreateEvent`.
3. Replaced blocking `ConnectNamedPipe(hPipe, NULL)` with an async call passing an `OVERLAPPED` struct linked to `hConnectEvent`.
4. Issued a zero-byte async `ReadFile` to arm the OS: it will signal `hConnectEvent` the moment the pipe's connection state changes (disconnect or incoming data).
5. Both the connection wait and the health-check loop now use `WaitForMultipleObjects(2, events, FALSE, INFINITE)` — sleeping at 0% CPU until the OS fires one of the two events.
6. `Stop()` now signals `hStopEvent` to instantly wake `WaitForMultipleObjects` for a clean, guaranteed shutdown.

**Result:**
| Metric | PeekNamedPipe (K-15 fix) | Overlapped I/O (K-16) |
|---|---|---|
| Disconnect latency | 100ms max | <1ms |
| Clean shutdown | Eventually | Instant |

---

## Group L: Phase 4.5 — Governor, OCR Synthesis & RAG Context

### Trap L-1: Process Governor Hardcoded Array Bypass
**Problem:** The process governor (`MemoryManager::ThrottleDistractors`) relied on a hardcoded string array (`DISTRACTOR_APPS`) to target and throttle processes like Spotify during deep work. However, the system's core intelligence (`EMA scores`) tracked process priorities dynamically. As the user installed new apps (e.g., Discord, WhatsApp desktop), the C++ engine ignored them because they weren't explicitly hardcoded, leaking CPU cycles during deep coding sessions.
**Fix:** Removed the hardcoded array entirely. Modified the `ThrottleDistractors` function to query the dynamic `emaScores` hash map. Apps with an EMA score strictly greater than `0.0` but below a `DISTRACTOR_THRESHOLD` (e.g., `< 0.25`) are now dynamically identified and throttled. Unknown background processes (`score == 0.0`) are left untouched, ensuring safe OS operation while creating a true "Self-Training Governor."

### Trap L-2: Llama-Server CUDA Stack Overflow on Massive Contexts
**Problem:** A UIA screen capture of a full IDE or browser window can easily exceed 20,000 characters. When this raw string was fed directly into `Gemma` via the Ollama API, the internal `llama-server` process suffered a fatal `0xc0000409` (Stack Buffer Overrun) CUDA error and crashed.
**Fix:** Implemented Section-Wise Synthesis. `flush_worker.py` now explicitly chunks massive UIA returns using the `===SECTION===` delimiters. Each section is capped at `3000` characters and passed to the LLM individually. This keeps the prompt safely within the `4096` token context window, completely eliminating the stack overflow crashes.

### Trap L-3: The Greedy Extraction Logic (Code Loss)
**Problem:** After moving to Section-Wise Synthesis, the AI Engine was configured to use a "best-wins" selection strategy: it generated JSON for each section, but only returned the longest output to `flush_worker.py`. On LeetCode, the section containing the problem statement generated a larger output than the section containing the user's C++ code snippet. The AI Engine ruthlessly discarded the code snippet, blinding Jugnu to the user's actual work.
**Fix:** Removed the best-wins logic. `synthesize_ocr_extractions` now returns a `list[str]` containing *all* valid JSON outputs from every section. `flush_worker.py` loops over this list and commits every valid knowledge doc to the database, ensuring no semantic context is lost.

### Trap L-4: Destructive JSON Truncation During Merges
**Problem:** To prevent database bloat, `save_knowledge_doc` checks the vector database for existing docs with a cosine similarity `< 0.30`. If it finds a match, it calls `merge_knowledge_docs` via the LLM. However, the merge prompt aggressively truncated the `existing_json` payload to `1000` characters to save tokens. This literally sliced the JSON string in half, feeding broken syntax (e.g., `{"content": "abc...`) to the LLM, which caused it to hallucinate or drop the data entirely.
**Fix:** Increased the string truncation limits to `4000` characters to accommodate full JSON payloads, and doubled the `num_ctx` to `8192` tokens for the merge API call to ensure the LLM has enough memory to digest both full documents simultaneously.

### Trap L-5: Explorer.EXE Poisoning the Idle Context
**Problem:** If the user is coding, but hovers their mouse over the Windows Taskbar immediately before going AFK, the `USER_IDLE` event triggers with `Explorer.EXE` as the current foreground process. When Python receives this, it generates an empty KNN search query (because Explorer has no coding context) and fails to fetch relevant RAG documents.
**Fix:** 
1. In `win_monitor.cpp`, introduced `lastMeaningfulApp`. This string is only updated *after* the Explorer/OS transient filters pass. The idle timer uses this variable instead of the raw active window.
2. In `ipc_client.py`, if an OS noise app bypasses C++, we fallback to `state.get_last_coding_app()`. Crucially, we also set `state.current_app = fallback` so that subsequent DB queries (`generate_prompt_context`) fetch the correct live OCR data.

### Trap L-6: Infinite Re-Synthesis Loop on Duplicates
**Problem:** The `flush_worker.py` clears processed rows from the `ocr_buffer` by checking if `save_knowledge_doc` returned `True`. However, if the LLM successfully synthesized docs, but the Embedder identified them as semantic duplicates and skipped saving them, `save_knowledge_doc` returns `False`. Because of this, the `row_id` was never appended to the deletion queue, causing the same OCR row to be re-processed by the LLM every 60 seconds infinitely.
**Fix:** Restructured the deletion logic. The `row_id` is now explicitly appended to the deletion queue as long as the synthesis phase ran (whether the docs were saved, merged, or discarded as duplicates), ensuring raw staging rows are always purged after one attempt.

### Trap L-7: The Markov Amnesia Trap (Boot Initialization)
**Problem:** The C++ `MemoryManager` tracked Markov transitions and EMA priorities in lightning-fast RAM `std::unordered_map`s. However, during initialization, the C++ engine failed to `SELECT` and load the historical data from the SQLite database. Every time Jugnu was restarted, the RAM maps initialized empty, completely erasing all long-term learned behavioral patterns and treating every day as Day 1.
**Fix:** Implemented `DBHandler::LoadMarkovChain()` and `DBHandler::LoadEMAScores()` to properly hydrate the RAM maps at startup before the `WinMonitor` hooks attach, ensuring continuity of learned intelligence.

### Trap L-8: The Ghost Flusher Deadlock
**Problem:** The `MemoryManager` was supposed to dump the hot RAM state (EMA and Markov chains) to SQLite every 30 minutes to prevent data loss on a crash. However, the background flush loop was silently hanging because it attempted to take an exclusive database `std::mutex` lock that was already held by the active `WinMonitor` thread writing a rapid stream of `app_switch` events.
**Fix:** SQLite WAL mode natively handles concurrent writers and readers, but the C++ application-level `std::mutex` was unnecessarily blocking threads. Refactored the locking strategy in `MemoryManager` to use highly granular, map-specific `std::shared_mutex` for RAM protection (allowing multiple readers/one writer for the maps) while letting SQLite handle its own internal locking for disk writes.

### Trap L-9: The Unhandled Exception Data Vaporization
**Problem:** Even with the 30-minute background flush, if the C++ engine crashed (e.g., due to an access violation or unhandled exception) at minute 29, nearly half an hour of learned Markov transitions and EMA scores would be permanently vaporized from RAM.
**Fix:** Implemented a global exception handler using Windows API's `SetUnhandledExceptionFilter()`. When a fatal crash occurs, the OS pauses the process destruction and passes execution to our custom handler. The handler immediately calls `MemoryManager::FlushMarkovEdges()` and `MemoryManager::FlushEMAScores()` to forcibly write the hot RAM state to SQLite one last time before allowing the process to gracefully die, achieving near-zero data loss.
