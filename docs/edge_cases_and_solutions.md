# Jugnu — Edge Cases & Solutions

> This document is a living reference. Every case here must be solved before we write a single line of classification or context-detection code.

---

## Category 1: Classification Edge Cases

### EC-C1 — The Context Shift Problem
**Situation:** Chrome is open. The user switches tabs from LeetCode (CODING) to YouTube music (ENTERTAINMENT). The foreground app is still `chrome.exe`. Jugnu only sees the title of the currently focused window, not all open tabs.

**Solution:**
- We do NOT cache by `app_name` alone for browsers. We cache by `domain` extracted from the window title.
- Each time a SWITCH event fires, we re-evaluate the domain from the title even if the app name is the same.
- For browsers, the cache key format is: `browser::<extracted_domain>` (e.g., `browser::youtube.com`).
- Domain extraction logic lives in Python. It's a simple string parse — no LLM needed.

---

### EC-C2 — The Ambiguous Title Problem
**Situation:** YouTube title `"Array problems in C++ - Striver"` = STUDYING. Title `"Lo-fi beats to relax"` = ENTERTAINMENT. Both are on YouTube. The LLM might sometimes get this wrong.

**Solution:**
- For known ambiguous domains (YouTube, Twitter/X, Reddit), we ALWAYS classify by title, never by domain alone.
- We add a `confidence` field to the `app_classifications` table. If the LLM itself says it is not certain, confidence is LOW.
- LOW confidence classifications are treated as temporary — they are NOT permanently cached.
- They expire after one session and the LLM is asked again next time.

---

### EC-C3 — The Same App, Different Context Problem
**Situation:** VSCode open with code → CODING. VSCode open to edit a markdown diary → not coding. Terminal running dev server → CODING. Terminal running `spotify-tui` → ENTERTAINMENT.

**Solution:**
- For terminal apps (`pwsh.exe`, `cmd.exe`, `wt.exe`), the title usually contains the running command or directory path. We use the title to classify, not the app name.
- For editors (VSCode), the title contains the filename. We check the file extension in the title (`.py`, `.cpp`, `.js` = CODING; `.md`, `.txt` in a non-project path = UTILITY).
- This is a title-based classification rule that runs BEFORE the LLM call, so we save LLM tokens.

---

### EC-C4 — The Transition Reclassification Problem
**Situation:** LeetCode tab cached as CODING at 9am. Same tab still open at 11pm when user is asleep. The classification is stale.

**Solution:**
- Classifications have a `last_seen` timestamp in the DB.
- We do not reclassify based on age alone — that would cause annoying re-queries.
- Instead, the idle timer has two phases:
  - **Short idle (5-30 mins):** User is probably thinking. Stay in current mode.
  - **Long idle (>30 mins):** User is AFK. Jugnu enters **Sleep Mode** — all timers stop, no classifications fire.
- When the user comes back (any input detected), Jugnu does a fresh classification of whatever window is currently active.

---

## Category 2: Idle Timer Edge Cases

### EC-I1 — The "Thinking Hard" Problem
**Situation:** User is staring at a hard problem for 10 minutes without touching mouse or keyboard. They are in deep flow state. Jugnu interrupts them with a buddy card. This is the worst possible outcome.

**Solution:**
- The idle timer threshold must NOT be a fixed 5 seconds. It needs to be learned.
- We introduce a **Confidence Window** concept:
  - Short idle (5–60s): Do NOT trigger. User is probably just reading or thinking.
  - Medium idle (60s–5min): Trigger only if user made ZERO progress recently (no recent file saves, no recent typing bursts detected).
  - Long idle (>5min): High confidence the user is genuinely stuck. Trigger.
- The exact thresholds should be stored in a `user_preferences` table in the DB so the user can tune them later via onboarding or a config file.

---

### EC-I2 — The "Reading Carefully" Problem
**Situation:** User is reading documentation without scrolling. Idle for 3 minutes. They are STUDYING, not stuck.

**Solution:**
- This is handled by the classification pipeline. If the current context category is STUDYING, the idle timer does NOT trigger the AI Buddy Card. Instead, it triggers "Recording Mode" which will quietly save the page content to the Vector DB (Phase 4).
- The action taken on idle depends entirely on the current category, not just the idle duration.

---

### EC-I3 — The "AFK for Real" Problem
**Situation:** User walks away for 30 minutes. The idle timer fires every second. When they return, hundreds of events are queued and Jugnu hammers the LLM.

**Solution:**
- We implement a **Sleep Mode** hard cutoff. If idle > 30 minutes, Jugnu enters Sleep Mode.
- In Sleep Mode: the StuckTimerThread calls `Sleep(60000)` (1 minute intervals) instead of `Sleep(1000)`. Effectively zero CPU/battery impact.
- `hasTriggered` is set globally. NO new events fire until the user moves the mouse.
- On wake (first input detected after sleep), Jugnu waits 3 seconds, re-classifies the current window, and resumes normal operation.

---

### EC-I4 — The "Meeting" Problem
**Situation:** User is in a Zoom/Teams call on one monitor, their code is on the second monitor. The foreground app is `Zoom.exe`. The stuck timer correctly identifies them as NOT in a coding context. But the user might mentally be coding during a pair programming session.

**Solution:**
- This is an inherently hard problem. We cannot see what the user is "thinking about".
- Our solution: If the user is on a video-call app (Zoom, Teams, Meet), Jugnu classifies it as UTILITY and goes quiet. It does NOT try to help with coding.
- In the future, when we build the onboarding system, we can ask: *"Do you sometimes pair-program on Zoom? Should I stay active during calls?"* — and store the answer in `user_preferences`.

---

## Category 3: LLM Classification Edge Cases

### EC-L1 — The "Confident but Wrong" Problem
**Situation:** LLM misclassifies `Antigravity IDE.exe` as ENTERTAINMENT because it's never heard of it. This gets permanently cached. Jugnu ignores the user forever in their primary coding tool.

**Solution:**
- We store `confidence` in the DB (HIGH / LOW).
- Any classification of an unknown/obscure app is automatically tagged as LOW confidence.
- LOW confidence entries prompt a user verification the next time that app/context is detected: *"I classified [App] as ENTERTAINMENT — is that right?"*
- If the user says NO, we delete the cache entry and re-classify with the user's input as a hint.
- This is the core of the "Ask don't assume" design principle.

---

### EC-L2 — The "Stale Cache" Problem
**Situation:** Full YouTube title cached as the key. Every new video is a new cache miss. Cache becomes useless.

**Solution:**
- Already addressed in EC-C1 and EC-C2. Cache key is `browser::<domain>` not the full title.
- For `youtube.com` specifically, we always re-run the title classification because the content changes per video. But instead of querying the LLM every time, we use a fast local rule first:
  - Title contains `("tutorial", "course", "lecture", "learn", "explained", "how to", "A2Z", "roadmap")` → STUDYING.
  - Everything else on YouTube → ENTERTAINMENT.
  - Only if the title is genuinely ambiguous do we ask the LLM.

---

### EC-L3 — The "Rate of Classification" Problem
**Situation:** User rapidly browses 10 new websites in the morning. 10 simultaneous LLM calls. System freezes.

**Solution:**
- Classification runs on a dedicated **background thread** in Python. The main IPC listener loop never blocks on it.
- Classifications are queued (a Python `queue.Queue`). A single background worker processes them one at a time.
- While a classification is pending, the system defaults to UTILITY (do nothing). When the result comes back, it updates the state for the next idle event.
- Maximum queue size: 5 items. If the queue overflows, we discard old items — there is no point classifying a website the user already left.

---

## Category 4: Database & Persistence Edge Cases

### EC-D1 — The "Write Conflict" Problem
**Situation:** C++ writes to `jugnu.db` (logging app switches) simultaneously with Python reading/writing (caching classifications). SQLite `database is locked` errors occur.

**Solution:**
- Enable SQLite **WAL Mode** (Write-Ahead Logging) in both the C++ `db_handler.cpp` and Python's `sqlite3` connection: `PRAGMA journal_mode=WAL;`.
- WAL mode allows unlimited concurrent readers and one writer at a time without locking.
- In addition, we clearly define ownership: C++ owns the `app_switches` and `app_paths` tables. Python owns the `app_classifications` and `user_preferences` tables. They never write to each other's tables.

---

### EC-D2 — The "Jugnu Watching Itself" Problem
**Situation:** Already seen! GhostWriter sees `jugnu.db` change, sends `FILE_SAVED` to Python, Python tries to read the binary `.db` file as UTF-8 text, crashes.

**Solution:**
- Fix the C++ `file_watcher.cpp` to ignore a blacklist of file extensions: `.db`, `.db-journal`, `.db-wal`, `.db-shm`, `.pyc`, `.log`.
- This is a simple string filter in C++ before the file change event is sent to Python.

---

### EC-D3 — The "Unlearning" Problem
**Situation:** Jugnu permanently cached a wrong classification. There is no mechanism to correct it.

**Solution:**
- A future `jugnu config` CLI command will let the user view and delete cache entries.
- More immediately: the LOW confidence + user verification flow (EC-L1) handles the most critical correction case automatically.
- We also store a `source` field in `app_classifications`: `AI_CLASSIFIED` vs `USER_CONFIRMED`. User-confirmed entries are never auto-deleted or reclassified.

---

### EC-D4 — The "Cold Start" Problem
**Situation:** First launch. Zero cache. User switches apps 20 times in 5 minutes. 20 LLM classification calls queue up on first boot.

**Solution:**
- On first launch, we ship a **seed classification file** (`defaults.json`) with 20–30 common apps pre-classified (Chrome, VSCode, Zoom, Steam, Spotify, etc.). This is loaded into the `app_classifications` DB table on first run.
- These are tagged as `source = SEED` and confidence = HIGH for well-known apps.
- This eliminates cold-start LLM thrashing for common tools.

---

## Category 5: User Interaction Edge Cases

### EC-U1 — The "When to Ask" Problem
**Situation:** Jugnu is confused. If it asks every time, it's annoying. If it never asks, it makes permanent mistakes.

**Solution:**
- Jugnu only asks at **natural transition moments**: when the user switches apps, not while they are typing or in deep focus.
- It uses a **cooldown**: Never ask the user a question twice within 10 minutes, even if it encounters 10 unknown apps in that window.
- Priority queue: Only ask about apps where the classification decision actually matters (potential CODING or STUDYING contexts). Don't bother asking about `notepad.exe`.

---

### EC-U2 — The "How to Ask" Problem
**Situation:** We have no GUI. Jugnu only prints to a terminal the user may not be watching.

**Solution:**
- Short term: When Jugnu needs to ask something, it prints a very visible message to the terminal with ASCII art border and waits for input for 10 seconds. If no response, it defaults to UTILITY and moves on.
- Medium term (Phase 5): Use the Windows `MessageBox` Win32 API from C++ to fire a native OS dialog. This is a one-line C++ call, requires no GUI framework, and appears on top of everything.
- Long term: Build a proper system tray icon and notification popup.

---

### EC-U3 — The "Onboarding" Problem
**Situation:** Jugnu doesn't know anything about the user on first launch. What languages do they use? What are their goals? What platforms do they prefer?

**Solution:**
- A one-time interactive terminal chat on first launch (a Python `input()` loop).
- Jugnu asks 5–7 key questions: tech stack, preferred IDEs, competitive programming platforms, study resources used, whether they want proactive interruptions or on-demand only.
- Answers are stored in a `user_preferences` table in `jugnu.db`.
- These preferences are loaded into `state_manager.py` at startup and used to augment LLM classification prompts, making them far more accurate from day one.

---

## Category 6: Performance Edge Cases

### EC-P1 — The "Rapid Fire App Switch" Problem
**Situation:** User Alt+Tabs rapidly between 5 windows. 5 classification events queue up in 2 seconds.

**Solution:**
- We implement a **debounce** on the SWITCH event in Python. When a SWITCH event comes in, wait 500ms. If another SWITCH arrives in that window, cancel the first and restart the timer. Only classify the window the user actually settled on.
- This is a classic debounce pattern. Zero LLM calls for rapid switching.

---

### EC-P2 — The "Large File" Problem
**Situation:** User saves a 5MB minified JS file. GhostWriter sends `FILE_SAVED`. Python reads the whole file into memory as context for the AI. Memory spikes.

**Solution:**
- `state_manager.py` enforces a hard limit: read a maximum of **4KB** from any file. For code files, we read the first 2KB (top of file — imports, class declarations) and the last 2KB (recent changes).
- Files over a size threshold (e.g., 100KB) are noted as "large file saved: [filename]" in the context but their contents are NOT loaded into memory.

---

### EC-P3 — The "Battery Drain During Sleep" Problem
**Situation:** User walks away. Jugnu continues running at normal 1-second poll rate while the user is asleep. Wastes battery.

**Solution:**
- Covered by EC-I3 (Sleep Mode). When in Sleep Mode, the C++ thread sleeps for 60 seconds between checks instead of 1 second.
- Additionally, when Sleep Mode is active, Python's classification queue is paused entirely.

---

### EC-P4 — The "LLM Inference Blocking the UI" Problem
**Situation:** LLM takes 3-4 seconds to classify a new app. During that time, if inference blocks the main thread, the IPC pipe stops being read. C++ tries to write more events, the pipe buffer fills up, and the whole system stalls.

**Solution:**
- LLM inference ALWAYS runs in a background thread (a Python `ThreadPoolExecutor` with max 1 worker). 
- The main IPC loop is completely non-blocking. It reads events and dispatches them to the queue instantly.
- This means there is zero coupling between the IPC read speed and the LLM inference speed.

---

## Additional Edge Cases (Not Covered by User Yet)

### EC-X1 — The "Notification Pop" Problem
**Situation:** User is deep in code. Discord sends a notification. Windows briefly steals focus to show the notification toast. Jugnu sees a SWITCH to `Discord.exe` for 300ms, then back to the IDE. Jugnu wastes an LLM classification call on Discord and may mis-trigger the stuck timer reset.

**Solution:**
- The debounce from EC-P1 handles this. A 500ms debounce means micro-focus-steals from notification toasts are completely invisible to the classification system.

---

### EC-X2 — The "Screen Lock / Suspend" Problem
**Situation:** User locks their screen or laptop goes to sleep. Windows fires events but no meaningful window title is available. Jugnu might try to classify the lock screen as an app.

**Solution:**
- When a SWITCH event arrives with an empty or system-level process (`LogonUI.exe`, `LockApp.exe`), Python immediately sets state to UTILITY and activates Sleep Mode. No classification needed.

---

### EC-X3 — The "Multiple Monitors" Problem  
**Situation:** User has two monitors. They are watching a YouTube tutorial on the left screen while coding on the right. The "foreground" window reported by Windows is whichever one was last clicked. If they click the YouTube video, Jugnu thinks they switched to STUDYING.

**Solution:**
- This is a fundamental Windows API limitation. We cannot reliably track two foreground contexts simultaneously with our current hook.
- Pragmatic solution: We trust the foreground window as the "active intent" of the user. If they click YouTube, they intend to watch it. If they click back on their editor, they intend to code. This is actually usually correct user behavior.
- We document this as a known limitation in the future UI.

---

### EC-X4 — The "Jugnu Learning Wrong Habits" Problem
**Situation:** User uses their laptop for a week, then lends it to a friend for a day. The friend's browsing behavior permanently pollutes Jugnu's classification cache.

**Solution:**
- User-confirmed classifications (source = `USER_CONFIRMED`) are never touched.
- AI-classified entries older than 30 days without any `USER_CONFIRMED` override are soft-deleted and reclassified fresh.
- Long term: Jugnu could have a concept of a `user_session` so it can detect anomalous behavior patterns.

---

## Summary: Core Design Principles Derived From All Edge Cases

| Principle | Implementation |
|---|---|
| **Never block on the LLM** | All inference in background threads |
| **Never interrupt flow state** | Multi-phase idle timer with meaningful thresholds |
| **Ask don't assume** | LOW confidence = user verification required |
| **Cache smart, not dumb** | Domain-level cache keys for browsers, local rules before LLM |
| **Sleep when idle** | Hard 30-min AFK cutoff → 60s poll interval |
| **Debounce rapid events** | 500ms debounce on all SWITCH events |
| **No hardcoding** | All thresholds and preferences in DB `user_preferences` table |
| **Clear DB ownership** | C++ owns monitoring tables, Python owns intelligence tables |
| **Seed on cold start** | Ship `defaults.json` to avoid first-run LLM thrashing |
| **User always wins** | `USER_CONFIRMED` classifications are sacred, never auto-overwritten |


## UI Integration Edge Cases (Phase 1.5)
### 1. The PyWebView Headless Start Trap
- **Case**: PyWebView (`webview.start()`) throws exceptions and refuses to start the message loop if no window is explicitly created beforehand.
- **Solution**: Spawning a "dummy" master window with `hidden=True` immediately before `start()`. This tricks the event loop into initializing while allowing Jugnu to remain a completely invisible background service until explicitly triggered.

### 2. Thread Deadlock on UI Launch
- **Case**: When the Python script detects user idleness, popping a PyWebView dialog halts the thread execution until the user clicks an option. Because the IPC read loop is on the same thread, telemetry drops completely.
- **Solution**: Delegated the UI invocation to a background `threading.Thread()`. The UI thread safely uses `Event().wait()` to simulate blocking for the user's answer, while the main thread continuously parses the IPC named pipe.



### 3. Display Scaling UI Clipping
- **Case**: Frameless window borders on Windows often reserve hidden padding due to display scaling (125%/150%), causing fixed-height bottom UI buttons to be chopped in half.
- **Solution**: Padding the explicit `webview.create_window` height parameter by a safe margin (e.g., +30px) guarantees the browser rendering engine maintains a safe internal margin for HTML components.

### 4. Package Manager OS Noise (Win32)
- **Case**: Installing packages via `uv` triggers hundreds of Win32 `FILE_ACTION_MODIFIED` events in the virtual environment.
- **Solution**: Strict, explicit string filtering in C++ ignoring `.git`, `.venv`, `.db`, `pyproject.toml`, and `.tmp`.

### 5. The Python Subprocess OCR Trap
- **Case**: Originally, OCR was planned via Python `mss` taking a screenshot and spawning a `subprocess.Popen` to PowerShell or Tesseract to extract text. This caused massive CPU spikes, disk write thrashing, and high latency.
- **Solution**: Complete compiler migration to **MSVC (Visual Studio Build Tools)**. Jugnu now uses `Windows.Media.Ocr` via native C++/WinRT. It takes a screenshot directly into a RAM buffer (no disk writes) and processes it on the GPU, dropping OCR overhead to nearly 0%.

---

## Category 7: OCR & Data Processing Edge Cases (Phase 3 Updates)

### EC-O1 — The "Incomplete SQL Input" Trap
**Situation:** When defining the `CREATE TABLE ocr_buffer` query in C++ using a raw string literal `R"()"`, we accidentally forgot the closing `);` inside the string. The C++ compiler compiled it perfectly, but SQLite crashed at runtime with `[DB] SQL error: incomplete input`.
**Solution:** Always manually test multi-line SQL queries in a SQLite REPL before embedding them in C++ raw literals. 

### EC-O2 — The "Offline Embedder Crash" Problem
**Situation:** The `SentenceTransformer` class defaults to pinging HuggingFace (`huggingface.co`) to check for updated config files on boot. If the user is on an airplane or offline, the Python script crashed entirely with `[Errno 11001] getaddrinfo failed`, tearing down the IPC Named Pipe and deadlocking the system.
**Solution:** We implemented a custom `_is_online()` check using Python's raw `socket` to try an 80-port connection to HuggingFace. If it fails, we explicitly pass `local_files_only=True` to the `SentenceTransformer` constructor, forcing it to load from the local `.cache` folder and bypass all network calls.

### EC-O3 — The "Database Loop Closure" Indentation Trap
**Situation:** In the `flush_worker.py` daemon, the `conn.close()` statement was accidentally indented one tab too deep, putting it *inside* the `for chunk in chunks:` loop. The daemon successfully processed the very first chunk, closed the database, and then instantly crashed with `Cannot operate on a closed database` on the second chunk.
**Solution:** Strictly verify Python indentation levels when managing persistent DB connections across batches, or use `with sqlite3.connect(...) as conn:` context managers to guarantee safe connection lifecycle handling.

### EC-O4 — The "Stale C++ Engine" Trap
**Situation:** We updated `db_handler.cpp` to create the new `ocr_buffer` table, but forgot to recompile `jugnu.exe`. When we started the Python script, it connected to the *old* running C++ executable, which hadn't created the table. Python instantly crashed with `no such table: ocr_buffer`.
**Solution:** Always maintain strict build discipline. Stop the C++ background daemon, run `ninja`, and restart `jugnu.exe` whenever a SQLite schema is modified in the C++ layer.

### EC-O5 — The "Unprintable OCR Pixel Garbage" Trap
**Situation:** The OCR engine sometimes interprets weird UI textures or scrollbars as ASCII control characters (like `0x1B` ESC). Sending these over the IPC pipe to Python caused `json.loads()` to throw a `JSONDecodeError: Invalid control character`.
**Solution:** The C++ IPC layer explicitly checks if `static_cast<unsigned char>(c) < 0x20` and manually converts those raw bytes into safely escaped Unicode sequences (`\u001b`) before sending them across the pipe.

---

## Category 8: Python Inference Pipeline Edge Cases (Phase 4 Updates)

### EC-I1 — The "SQLITE_BUSY RAG Crash" Trap
**Situation:** The C++ engine uses a massive `BEGIN TRANSACTION` block to flush RAM to disk instantly (100,000+ inserts per second). While this lock is held, if Python tries to run a background semantic search via `embedder.py`, SQLite throws a fatal `database is locked (SQLITE_BUSY)` error and crashes the entire Python daemon.
**Solution:** Pass `timeout=5.0` to the Python `sqlite3.connect()` calls. Instead of crashing, Python now gracefully yields and waits up to 5 seconds for C++ to finish its bulk flush.

### EC-I2 — The "Flash Attention PDL" Trap
**Situation:** `ollama` supports Flash Attention, which is a brilliant VRAM optimization. However, on consumer-grade mobile cards (like RTX 4050), requesting Flash Attention frequently caused the NVIDIA driver to crash with a Page Fault (PDL) or `cuMemAlloc` error during the first inference run.
**Solution:** Hardcoded `"flash_attn": False` in all `ollama.chat()` calls inside `ai_engine.py`. Stability > Marginal speed gains. We also implemented a dummy 1-token `_warmup()` call to silently absorb any residual cold-start crashes.

### EC-I3 — The "Pydantic API Breakage" Trap
**Situation:** The `ollama` pip package updated its architecture, migrating from returning standard Python dictionaries to returning Pydantic model objects. Existing `response.get("message")` calls suddenly threw `AttributeError` on users with updated environments.
**Solution:** Defensive Duck Typing. We changed the parsing logic to `hasattr(response, 'message')`. This gracefully handles BOTH the legacy dictionary API and the new Pydantic API.

### EC-I4 — The "Battery Drain During Deduplication" Trap
**Situation:** The Python `FlushWorker` wakes up every 60 seconds, grabs the `ocr_buffer`, and feeds chunks to Gemma to extract knowledge. If the user is on battery power, doing GPU inference every 60 seconds will kill the laptop in under an hour.
**Solution:** Used Python's `ctypes` to bind directly to the native Win32 `GetSystemPowerStatus` API. The `FlushWorker` checks if `ACLineStatus == 1` before doing any work. If on battery, it aborts the cycle immediately.

### EC-I5 — The "Custom Problem Override" Trap
**Situation:** User is stuck. Jugnu pops a UI notification loaded with vector context about the `server.py` file currently on their screen. The user clicks "Yes, I need help" but types: *"Actually, how do I configure Docker?"* Jugnu tries to answer the Docker question using the `server.py` context and hallucinates badly.
**Solution:** Implemented the "Custom Problem RAG Override" in `notification.py`. If the user types a custom question, Jugnu throws away the pre-fetched screen context, dynamically generates a brand new search query, queries the vector database for Docker info, and answers correctly.

### EC-I6 — The "Context Window Confusion" Trap
**Situation:** Feeding 4,000 characters of raw OCR UI noise directly into a 4B parameter model overwhelms the attention mechanism. Gemma hallucinates or skips crucial code blocks.
**Solution:** Implemented a context-aware `_chunk_text()` algorithm that strictly slices OCR text into 500-character windows, snapping to natural text boundaries (`\n\n` -> `\n` -> `. `). Gemma runs sequentially over these tiny, highly focused windows, extracting flawlessly.


### Edge Case 10: Process Snapshot CPU Spikes
**Scenario:** Jugnu uses CreateToolhelp32Snapshot to get a list of all processes on the machine to throttle distractors (like Spotify). Originally, this was called on *every* EVENT_SYSTEM_FOREGROUND trigger.
**Problem:** If the user dragged a window across monitors or Alt-Tabbed rapidly, generating 20 events per second, Jugnu forced the OS to generate 20 kernel snapshots per second. This spiked CPU usage and induced micro-stutters.
**Solution:** A static guard lastThrottledFor was added to ensure the snapshot is ONLY taken when the active application *changes*, cutting kernel calls by 99%.



### Edge Case 10: Process Snapshot CPU Spikes
**Scenario:** Jugnu uses CreateToolhelp32Snapshot to get a list of all processes on the machine to throttle distractors (like Spotify). Originally, this was called on *every* EVENT_SYSTEM_FOREGROUND trigger.
**Problem:** If the user dragged a window across monitors or Alt-Tabbed rapidly, generating 20 events per second, Jugnu forced the OS to generate 20 kernel snapshots per second. This spiked CPU usage and induced micro-stutters.
**Solution:** A static guard lastThrottledFor was added to ensure the snapshot is ONLY taken when the active application *changes*, cutting kernel calls by 99%.

### Edge Case 11: Unescaped JSON String Injection
**Scenario:** App names are injected into the JSON payload string sent to Python.
**Problem:** A user opens a file named Say "Hello".txt. The manual C++ string concatenation payload = "{\"app\": \"" + title + "\"}" resulted in an unescaped double-quote inside the JSON value, permanently breaking Python's json.loads().
**Solution:** Enforced strict RFC-compliant JSON sanitization (EscapeJSON) on all strings before concatenation, converting " to \".

### Edge Case 12: Infinite PowerShell Hangs
**Scenario:** The jugnu_interact.py script waits for a .done marker file to know when LLM generation finishes.
**Problem:** If the AI backend crashes, the file is never created. The script loops 	ime.sleep(0.3) indefinitely, stranding a zombie PowerShell window on the user's desktop that requires Task Manager to close.
**Solution:** Implemented a strict explicit timeout counter. If 180 seconds elapse, the script prints an error and terminates itself.

### Edge Case 13: The 49-Day Uptime Integer Overflow
**Scenario:** A user leaves their PC running continuously without a reboot for 50 days.
**Problem:** `GetTickCount()` returns a 32-bit unsigned integer representing milliseconds since boot. At 49.7 days, this integer overflows and wraps back to zero. Subtracting the `LASTINPUTINFO` timestamp from an overflowed tick count results in a massive integer underflow, falsely triggering an endless loop of stuck timer events.
**Solution:** Migrated the system to `GetTickCount64()`, which utilizes a 64-bit integer that takes 584 million years to overflow.

### Edge Case 14: Ghost Thread Shutdown Hangs
**Scenario:** The user closes the Jugnu application while a background thread is waiting for a file save (`ReadDirectoryChangesW`) or a clipboard copy (`GetMessage`).
**Problem:** Setting `isRunning = false` is insufficient because the background threads are hard-blocked inside the Windows kernel. The application hangs in the background indefinitely ("Ghost Process").
**Solution:** Implemented explicit thread-waking maneuvers in the `Stop()` methods. The main thread now issues `CancelIoEx()` to wake the file watcher, and `PostThreadMessage(WM_QUIT)` to wake the clipboard listener, guaranteeing clean shutdowns.

### Edge Case 15: The Unescaped File Path Quotes
**Scenario:** A user names their code file with a double quote, e.g., `test"file.py`.
**Problem:** The original IPC JSON escaper only handled backslashes. Injecting `test"file.py` into `{"file": "..."}` broke the JSON syntax, instantly crashing the Python IPC decoder.
**Solution:** Replaced the ad-hoc string replacer with a robust `EscapeJSON` utility that strictly sanitizes quotes and invisible control characters.

### Edge Case 16: The Polyglot IDE Blindspot
**Scenario:** A developer is actively practicing dynamic programming algorithms on LeetCode using Chrome (`chrome.exe`).
**Problem:** The native C++ engine's `CAPTURE_APPS` list properly whitelisted `chrome.exe` and captured OCR perfectly. However, the Python pipeline evaluated user intent via a strict `CODING_APPS` whitelist that originally only contained traditional desktop IDEs (`code`, `clion`, `pycharm`). When the `USER_IDLE` event fired, Python concluded "Chrome is not a coding app", discarded the OCR context, and refused to spawn the AI helper.
**Solution:** Synchronized the Python `CODING_APPS` list with the C++ `CAPTURE_APPS` whitelist. Adding `chrome`, `msedge`, and `firefox` allows the system to recognize web-based developer workflows (LeetCode, HackerRank, documentation) as valid coding contexts that warrant proactive AI nudges.

### Edge Case 17: The IPC Ghost Connection
**Scenario:** The user kills the Python server with `Ctrl+C` while the C++ engine is idle (no app switches or file saves are happening). They then immediately try to restart the Python script.
**Problem:** The C++ `PipeListnerThread` was inside its `Sleep(100)` health-check loop. Since the C++ side was never sending data (idle), `WriteFile` never ran, so `isClientConnected` was never set to `false`. The background thread was stuck believing Python was still alive. When Python restarted and tried to connect to `\\.\pipe\jugnu_ipc`, the OS rejected it with `ERROR_PIPE_BUSY` because the C++ engine still held the old pipe handle open. Python would print "Pipe is busy. Retrying..." indefinitely.
**Solution (Phase 1):** Added active `PeekNamedPipe` health probing in the inner loop. This immediately detected the broken pipe even during idle.
**Solution (Phase 2 — Final):** Replaced the polling loop entirely with **Overlapped I/O** and `WaitForMultipleObjects`. The C++ thread now sleeps at 0% CPU and the OS wakes it up the instant the pipe breaks, with <1ms latency and zero kernel-call overhead.

### Edge Case 18: The Overlapped I/O Shutdown Deadlock
**Scenario:** The application is shutting down (`Stop()` is called) while the background `PipeListnerThread` is suspended inside `WaitForMultipleObjects(INFINITE)`.
**Problem:** With the original synchronous design, `isRunning = false` eventually broke the loop. With `WaitForMultipleObjects(INFINITE)`, the thread sleeps forever unless one of its event handles is explicitly signaled. Setting `isRunning = false` alone would never wake it up, causing the thread to hang and the process to never fully exit.
**Solution:** Added a dedicated `hStopEvent` Win32 Event handle. `Stop()` now calls `SetEvent(hStopEvent)` before disconnecting the pipe, which immediately wakes the sleeping thread. Inside the thread, `WaitForMultipleObjects` returns `WAIT_OBJECT_0 + 1` (the stop event index), and the thread breaks out of its loop and exits cleanly.

### Edge Case 19: The LLM Markdown Hallucination
**Scenario:** Gemma is instructed strictly with `think=False` and told to output pure JSON.
**Problem:** The LLM suffers from heavy training bias and routinely wraps its JSON output in Markdown code blocks (e.g., ` ```json `). This causes Python's `json.loads()` to instantly crash with `JSONDecodeError`, discarding the extracted context.
**Solution:** Bypassed JSON generation entirely. Modified the prompt to output explicit plain-text headers (`TOPIC:`, `TAGS:`, `CODE:`) and manually parsed the strings in Python. Text parsing guarantees a 100% success rate immune to LLM styling hallucinations.

### Edge Case 20: The WinRT COM Initialization Trap
**Scenario:** Utilizing C++/WinRT for hardware OCR while simultaneously maintaining the `IUIAutomation` legacy COM API for UI text extraction.
**Problem:** Calling `CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED)` worked for standard COM, but caused random `RPC_E_CHANGED_MODE` errors when WinRT background threads spawned, completely breaking the UI Automation tree traversal.
**Solution:** Replaced legacy COM initialization with modern `winrt::init_apartment(winrt::apartment_type::single_threaded)`, which correctly aligns the WinRT threading model with the legacy COM requirements for UIA.

### Edge Case 21: The UIA BFS Deduplication Trap
**Scenario:** Extracting text from Chrome or VS Code using `IUIAutomation`.
**Problem:** Naive recursive DOM traversal caused nested UI elements to print their text multiple times (e.g., reading a `div`'s innerText and then reading its child `span`'s innerText). This exploded the text payload size, flooding the IPC pipe and destroying the LLM context window.
**Solution:** Implemented a Breadth-First Search (BFS) traversal utilizing a `std::unordered_set<std::wstring>` (O(1) lookup) to strictly deduplicate text strings on the fly. Guaranteed pristine, non-overlapping text extraction.

---

## Category 9: RAG and Priority Governor Edge Cases (Phase 4.5 Updates)

### EC-R1 — The "Explorer Poisoning" Problem
**Situation:** User is coding in Chrome, their mouse hovers over the Windows Taskbar for a split second, and then they go idle. The `USER_IDLE` event triggers, but the active window is captured as `Explorer.EXE`. Jugnu tries to generate RAG context for the Windows shell instead of the actual coding problem.
**Solution:** A dual-layer fix: 
1. **C++ Layer:** Introduced `lastMeaningfulApp`. We track the foreground process, but only assign it to `lastMeaningfulApp` *after* all OS/Transient filters pass. The stuck timer uses this safe variable for the idle payload.
2. **Python Layer:** If `USER_IDLE` arrives with an OS noise app (or empty string), Python falls back to `state.get_last_coding_app()` and updates `state.current_app` so that the live OCR DB query fetches the correct screen text.

### EC-R2 — The "Greedy Problem Statement" Problem
**Situation:** When generating a Knowledge Doc from a massive screen capture (like LeetCode), the UIA splits the screen into multiple sections. The AI Engine was designed to use a "best-wins" strategy — evaluating all sections and keeping only the single longest extraction.
**Problem:** The LeetCode problem statement was longer than the user's C++ code snippet. The best-wins strategy ruthlessly discarded the user's actual code because it was shorter, making Jugnu blind to their solution.
**Solution:** Changed `synthesize_ocr_extractions` to return a `list[str]`. *Every* section that produces valid structured knowledge is preserved as a separate JSON doc, ensuring both the problem context and the code are captured.

### EC-R3 — The "Truncated JSON Merge" Trap
**Situation:** The `Embedder` prevents duplicate entries by running a cosine similarity check. If two docs share a topic (`dist < 0.30`), it merges them using the LLM.
**Problem:** The merge prompt brutally truncated the existing JSON doc to `1000` characters to save tokens. If the existing doc was a long problem statement, the JSON was abruptly sliced in half (`{"content": "Output...`), causing Gemma to completely hallucinate or drop the corrupted data during the merge.
**Solution:** Increased the merge prompt truncation limits to `4000` characters to comfortably fit full JSON representations, and doubled the inference context window (`num_ctx = 8192`) to guarantee the LLM has enough memory to digest both full documents.

---

## Category 10: OKF Pipeline & Context Saving Edge Cases (Phase 3 — Current)

These edge cases were discovered during the implementation of the Zero-IPC OCR pipeline and the structured OKF knowledge_docs system.

### EC-OKF1 — The "Infinite Reprocessing" Trap
**Situation:** The FlushWorker reads from `ocr_buffer`, calls Gemma to extract knowledge, and then deletes the processed row.
**Problem:** We originally deleted the row from `ocr_buffer` at the start of processing — before Gemma ran. If Gemma crashed mid-way (OOM, Ollama timeout), the row was already gone. The knowledge was lost permanently and there was no retry mechanism.
**Solution:** Changed the delete strategy to end-of-processing. A row is now only added to `ids_to_delete` after `save_knowledge_doc()` returns successfully. If an exception is caught, the row ID goes into `ids_failed` and stays in `ocr_buffer` to be retried on the next 60-second cycle.

---

### EC-OKF2 — The "URL Bar Poisoning" Trap
**Situation:** The UIA engine captures `Edit` control types from Chrome, which maps to code editors (Monaco). But Chrome's address bar is also a UIA `Edit` control.
**Problem:** The FlushWorker was routing all `Edit` controls as verbatim code, so the Chrome URL bar (`https://leetcode.com/problems/...`) was being saved as a "code snippet" tagged as `["code"]`. This polluted the knowledge vault with hundreds of URL entries.
**Solution:** Added a heuristic URL filter before the verbatim path. If the `Edit` text starts with `://`, `www.`, contains `leetcode.com` or `github.com` in the first 30 chars, or is a single word containing a `.` — it is silently skipped. Only the actual Monaco editor content passes through.

---

### EC-OKF3 — The "Semantic Anchor Embedding Failure" Trap
**Situation:** We embed the 1-2 sentence prose summary as the semantic anchor in `vec_knowledge`, not the raw code.
**Problem:** When the AI engine failed to generate a summary (empty `summary` field), the embedder fell back to embedding `topic + ""` — essentially just the topic string alone. This produced vectors that were nearly identical for all docs with the same topic, making KNN search useless and causing every document about "dynamic programming" to collide.
**Solution:** Added a fallback in `save_knowledge_doc()`: if `summary` is empty, embed `topic + content[:400]` instead. This ensures the vector always has enough semantic signal, even when Gemma fails to generate the prose anchor.

---

### EC-OKF4 — The "vec_knowledge Count Guard" Crash
**Situation:** The OKF similarity check runs a KNN query on `vec_knowledge` before deciding to insert or merge.
**Problem:** On a fresh install or after a database wipe, `vec_knowledge` has zero rows. Running `WHERE embedding MATCH ? AND k = 1` on an empty sqlite-vec virtual table throws a fatal exception instead of returning an empty result set. The entire FlushWorker cycle crashes on the very first knowledge doc it tries to save.
**Solution:** Added a `SELECT COUNT(*) FROM knowledge_docs` guard before the KNN call. If `count == 0`, the similarity check is skipped and the doc is inserted directly as a fresh entry. This mirrors the identical guard that was already in `save_memory()` for `vec_episodic`.

---

### EC-OKF5 — The "Duplicate Except Outer Try" Bug
**Situation:** The `save_knowledge_doc()` function in `embedder.py` has two code paths: a merge path (when a similar doc exists) and an insert path (when it's new).
**Problem:** During a refactor, a second `except Exception` block was accidentally placed at the same indentation level as the outer `try`, creating two separate `try/except` blocks. The inner merge path had no exception handling. Any error during merging (e.g., `json.loads` failure on a corrupted `ext_tags`) would propagate uncaught and crash the FlushWorker thread entirely.
**Solution:** Verified the exception handler covers the entire function body with a single outer `try/except`. The `tags` merge uses a nested `try/except` to handle malformed JSON gracefully, falling back to `old_tags = []` without breaking the outer flow.

---

### EC-OKF6 — The "Socket FD Leak on Startup" Trap
**Situation:** The `Embedder.__init__()` checks if the machine is online by connecting a raw socket to `8.8.8.8:53`.
**Problem:** The original implementation created a `socket.socket()` object and stored it in a variable but never explicitly called `.close()`. On every Jugnu startup, one OS file descriptor was leaked. On a machine running for weeks, this would eventually exhaust the process's FD limit.
**Solution:** Replaced the socket creation with a `with socket.socket(...) as _s:` context manager. The OS file descriptor is guaranteed to be closed the instant the `with` block exits, whether the connection succeeded, timed out, or raised an exception.

---

### EC-OKF7 — The "Unbounded Cache Memory Leak" Trap
**Situation:** The FlushWorker and Embedder both maintain in-memory caches (`_last_raw_by_app`, `_last_embedded`) to deduplicate screen captures and embeddings.
**Problem:** These caches were plain Python dicts with no size limit. In a long session where the user uses many different applications (each with a different `app_name` key), the cache grows unboundedly. Over days, this becomes a measurable RAM leak as OCR blobs (~2KB each) accumulate for every process the user has ever opened.
**Solution:** Added a `MAX_CACHE = 20` eviction policy in both caches. Before inserting a new entry, if the dict exceeds the limit, the oldest entry is evicted using `cache.pop(next(iter(cache)))`. This works because Python 3.7+ dicts are guaranteed to be insertion-ordered, so `next(iter(cache))` always returns the oldest key.

---

### EC-OKF8 — The "85% Screen Similarity False Skip" Trap
**Situation:** The FlushWorker uses `difflib.SequenceMatcher` to compare the current OCR dump to the last one processed for the same app. If similarity is > 85%, the AI extraction is skipped.
**Problem:** On LeetCode, the problem statement (top 80% of the screen) is static, but the user's code (bottom 20%) changes as they type. The difflib ratio was dominated by the stable problem text, consistently scoring > 85%, which caused Jugnu to skip Gemma extraction even when the user had just written a complete solution.
**Solution:** The 85% threshold is intentionally kept for full-screen OCR (fallback path) where the screen changes substantially between captures. For the structured UIA JSON path (primary path), similarity check is applied per-section rather than on the full blob, allowing changed `Edit` (code) sections to be processed even when the surrounding `Document` (problem statement) is unchanged.

---

### EC-OKF9 — The "Greedy Section Best-Wins" Problem
**Situation:** Early versions of the OKF pipeline extracted multiple UIA sections but used a "best-wins" strategy — only keeping the single extraction with the highest confidence score.
**Problem:** The LeetCode problem statement (a long `Document` control) consistently scored higher than the user's C++ code (a shorter `Edit` control). The strategy discarded the code entirely, making Jugnu blind to the user's actual solution — the most valuable thing to remember.
**Solution:** Changed `combine_sections()` to accept and return **all** valid extractions as a list. Every section that produces valid structured knowledge is preserved as an independent doc. Both the problem statement and the code snippet are saved to `knowledge_docs` as separate, linked entries.

---

### EC-OKF10 — The "30-Second Settle Time Race" Trap
**Situation:** The FlushWorker uses a 30-second settle-time filter: it only processes `ocr_buffer` rows that are at least 30 seconds old, giving C++ time to finish writing before Python reads.
**Problem:** When the user switches apps rapidly (e.g., switches to Chrome, types for 20 seconds, then switches back), C++ may write 3-4 rows within the 30-second window for the same app with evolving screen state. All 4 rows would pass the settle filter together in the next cycle, causing Gemma to extract the same progressively-built code snippet 4 times and creating duplicate knowledge docs.
**Solution:** The per-row difflib deduplication (EC-OKF8) handles this: when 4 rows for the same app arrive in the same cycle, the second, third, and fourth are compared against the cached text of the one before them and skipped if similarity is > 85%. Only the first (oldest) and last (most current) are ever extracted, and the merge logic in `save_knowledge_doc()` consolidates them into a single doc.

---

## Summary: Core Principles Added from OKF Edge Cases

| Principle | Implementation |
|---|---|
| **Delete only after success** | `ids_to_delete` populated at end of processing, not start |
| **Filter at the source** | URL bar heuristic prevents URL bar from entering the code path |
| **Always have a fallback embedding** | `topic + content[:400]` when `summary` is empty |
| **Guard KNN on empty tables** | `COUNT(*)` check before every sqlite-vec `MATCH` query |
| **Cap all caches** | `MAX_CACHE=20` eviction on every in-memory dict |
| **Per-section deduplication** | Similarity check on sections, not on full blobs |
| **Preserve all extractions** | Every section saved — no best-wins strategy |
| **Retry on failure** | Failed rows stay in `ocr_buffer` for next cycle |

---

## Category 11: The Zero-Overhead Hibernation Architecture (Phase 4)

These optimizations focus purely on how we eliminated CPU polling overhead in the native C++ monitor threads (`ScreenReader` and `StuckTimer`).

### EC-CPP1 — The "100ms Polling Spinloop" Overhead
**Situation:** The `StuckTimer` and `ScreenReader` threads need to know when the user is actively working in a Deep Work app (IDE/browser) vs when they are playing a game or watching a movie.
**Problem:** Early designs used a fixed `Sleep(100)` polling loop. The threads would wake up 10 times a second, query the foreground window, check if it was in the whitelist, and then go back to sleep. While low-impact individually, doing this constantly while the user is playing a CPU-heavy game steals L1 cache and micro-cycles from the game, leading to micro-stutters and battery drain.
**Solution:** Eliminated polling completely via the **Zero-Overhead Hibernation Architecture**.
1. We created a manual-reset Windows Event (`hDeepWorkEvent`).
2. When the user is NOT in a work app, both background threads call `WaitForSingleObject(hDeepWorkEvent, INFINITE)`. The Windows kernel suspends the threads with absolute **0% CPU usage** and zero cache evictions.
3. The `WinMonitor` foreground hook (which only fires exactly when the window changes) calls `SetEvent()` to instantly wake both threads when a whitelisted app is focused.
4. If a game is launched, `ResetEvent()` is called, and the threads seamlessly fall back into infinite hibernation.

---

### EC-CPP2 — The "Fixed Polling vs Dynamic Math" Trap
**Situation:** Even when the user *is* inside a Deep Work app, the `ScreenReader` needs to wait exactly 60 seconds of idle time before capturing the screen.
**Problem:** The initial implementation used a fixed `Sleep(2000)` loop while the user was active, waking up every 2 seconds to check `GetLastInputInfo()` and see if 60 seconds had passed. This is still a form of polling.
**Solution:** Switched to **Dynamic Sleep Math**. The thread queries `GetLastInputInfo()`, calculates exactly how much time is remaining until the 60-second threshold (`DWORD timeRemaining = 60000 - idleTime;`), and calls `Sleep(timeRemaining)`. The thread wakes up exactly once, at the exact millisecond the user has been idle for 60 seconds, completely eliminating mid-interval polling.

---

### EC-CPP3 — The "Infinite Shutdown Deadlock"
**Situation:** When Jugnu exits, `WinMonitor::Cleanup()` is called to terminate the hibernating background threads safely.
**Problem:** The cleanup function originally called `WaitForSingleObject(hStuckThread, 2000)` to wait for the thread to exit before signaling `hDeepWorkEvent`. But because the thread was blocked indefinitely on `WaitForSingleObject(hDeepWorkEvent, INFINITE)` while the user was in a non-work app, it would never wake up to check the `isRunning = false` flag. The cleanup function would hang for 2 seconds and then violently force-close the thread.
**Solution:** Reversed the shutdown order (Signal-Before-Wait). The cleanup function now explicitly calls `SetEvent(hDeepWorkEvent)` *first*. This instantly unblocks the sleeping threads. They wake up, see `isRunning = false`, and exit gracefully before the main thread calls `Wait`.

---

## Summary: Hibernation Architecture

| Principle | Implementation |
|---|---|
| **Zero-Overhead Hibernation** | `WaitForSingleObject(INFINITE)` completely suspends threads outside work apps. |
| **Event-Driven Wakeup** | `SetEvent()` / `ResetEvent()` triggered exclusively by the foreground OS hook. |
| **Dynamic Sleep Math** | `Sleep(timeRemaining)` eliminates polling even during active work sessions. |
| **Signal-Before-Wait** | Ensures sleeping threads can wake up to process termination signals. |

---

## Category 12: Knowledge Merging & Determinism Edge Cases (Phase 5)

### EC-OKF11 — The "Scroll Loss" Merging Trap
**Situation:** The user scrolls down in VSCode. The top 50 lines of code fall out of the UIA viewport. A new OCR snapshot is captured and sent to the embedder.
**Solution:** Implemented `difflib` opcode "Union Merge" in `embedder.py`. We treat any `delete` opcode as a "scroll off" event, not a deletion. We explicitly enforce a policy: **Never delete from knowledge, only add.** The only time code is safely overwritten is when the C++ Ghost Clipboard guarantees a `full_buffer` read.

### EC-OKF12 — The "Vector Identity Crisis" Trap
**Situation:** Two captures of the exact same LeetCode problem are embedded. Their vector distance is 0.35 because Gemma generated slightly different `topic` strings for each. They fail the similarity threshold and are treated as separate problems, duplicating the document.
**Solution:** Vector distance is designed for semantic search, not identity. Added hard-deterministic anchors in `embedder.py`. If the `window_title` or `file_path` matches an existing document exactly, we immediately merge them and bypass the fuzzy semantic threshold entirely.

### EC-OKF13 — The "Split-Deduplication" False Positive Trap
**Situation:** The user is on LeetCode. The problem statement (80% of the screen text) is completely static. The code (20%) is changing rapidly as they type. `difflib.quick_ratio()` evaluates the entire screen as >95% identical, so the `FlushWorker` skips extraction, missing the critical code changes.
**Solution:** Implemented Area-Wise Split Deduplication in `flush_worker.py`. We split the parsed UI JSON into `Edit` (Code) and `Document` (Page). We only skip extraction if *both* the Code is >95% identical AND the Page is >95% identical.

### EC-OKF14 — The "OCR-to-UIA Upgrade" Path
**Situation:** UIA fails to read a window, so Jugnu falls back to WinRT OCR. The resulting text is messy and tagged with `ocr`. Minutes later, the user interacts with the window, and UIA successfully captures pristine text for the exact same topic.
**Solution:** The merge logic in `embedder.py` explicitly checks tags (`is_old_ocr` and `not is_new_ocr`). If a pristine UIA capture matches a dirty OCR document, we completely overwrite the old OCR text with the pixel-perfect UIA strings, seamlessly upgrading the knowledge quality in real-time.

---

## Category 11: Practice Mode Edge Cases (Phase 6)

### EC-PM1 — The "Active Read-Only Tab" Pollution
**Situation:** User gets stuck and opens the "Solutions" or "Editorial" tab on LeetCode. UIA captures the perfect solution code on the screen. `flush_worker` saves this to the database, overwriting the user's authentic (but flawed) attempt with the perfect solution, ruining the practice history.
**Solution:** Added `read_only_tabs` regexes to the `CP_PLATFORMS` registry. `flush_worker` checks the `window_title`. If it matches a read-only tab, it actively strips the `code_snippet` from the capture, acting as a defensive read-only safeguard.

### EC-PM2 — The "Passive Reader" False STUCK
**Situation:** User opens a problem. The editor pre-fills with boilerplate (e.g., `class Solution { public: vector<int> twoSum(...) { } };`). The user spends 3 minutes just reading the problem. The idle timer fires. Jugnu thinks they are stuck coding and gives them a hint, even though they haven't written a single line of logic.
**Solution:** Developed the `_has_meaningful_code()` zero-LLM heuristic in `ipc_client`. It strips out common boilerplate keywords and scans for explicit control flow (`if`, `for`, `while`, `return`). If none exists, telemetry is forked to `CP_READING` (which suppresses the AI) rather than `CP_STUCK`.

### EC-PM3 — The "Ghost Hint" Trap (Persistent State)
**Situation:** User gets stuck at hint level 3 and eventually solves the problem. A month later, they return to practice the exact same problem. Jugnu remembers `hint_level = 3` from the database and instantly blurts out the final solution on their very first idle trigger, ruining the new practice attempt.
**Solution:** Fast substring heuristic for "Accepted" or "beats X%" footprints. When detected, `flush_worker` flips `is_solved=True` and resets the hint level to 0 in the SQLite `practice_sessions` table, guaranteeing a clean slate for future attempts.

### EC-PM4 — The "JSON Control Character Crash" Problem (Zero-DB IPC)
**Situation:** The ghost clipboard reads raw code from the active editor buffer and injects it into the `USER_IDLE` JSON payload to send over the IPC pipe.
**Problem:** Code strings routinely contain unescaped quotes (`"`), backslashes (`\`), newlines (`\n`), and tabs (`\t`). Injecting raw code directly into a JSON string breaks the JSON format instantly, crashing Python's `json.loads` upon receipt.
**Solution:** We added explicit string escaping in C++ before building the JSON payload in `win_monitor.cpp`. Every `\`, `"`, `\n`, `\r`, `\t` character is replaced with its escaped equivalent, and unprintable ASCII control characters (`< 0x20`) are stripped out.

### EC-PM5 — The "Multi-Replace Tool Deletion" Bug
**Situation:** When migrating `ipc_client.py`'s `_idle_handler_background` to accept `ipc_code` and prioritize it over the database snippet, an automated string replacement target was too broad.
**Problem:** The replacement logic unintentionally matched a broader block of code and deleted the core attempt detector (`if not _has_meaningful_code(editor_section):`) entirely.
**Solution:** Reverted and used exact line-number bound replacement targets to carefully weave `ipc_code` into the extraction fallback logic without nuking surrounding domain logic.

### EC-PM6 — The "GetLastCodeBuffer C++ Build Failure"
**Situation:** The `GetLastCodeBuffer()` method was added to the `ScreenReader` class to allow `StuckTimerThread` to access the RAM cache.
**Problem:** The method was added, but placed outside the `public:` access specifier block in `screen_reader.h`. The C++ compiler threw an access violation error during build because `win_monitor.cpp` could not call a private method.
**Solution:** Moved the static method definition explicitly under the `public:` block in the header file.

### EC-PM7 — The "LLM False Negative on Correct Logic" Problem
**Situation:** The user wrote a perfectly correct Minimax algorithm for LeetCode 486 (Predict the Winner), explicitly tracking `isPlayer1` state.
**Problem:** Gemma (`gemma4:e2b`) failed to recognize the logic as correct because it deviated from the typical condensed single-state textbook solution. Furthermore, Gemma hallucinated a constraint, insisting Player 1 needed a "strictly greater" score, when the problem explicitly stated ties go to Player 1.
**Solution:** This reinforces a critical architectural rule: Small, local LLMs cannot be trusted as authoritative code correctness validators for complex logic without explicit execution contexts. They hallucinate constraints. The `Practice Mode` should rely primarily on actual UIA "Accepted" badges for authoritative solve detection, and prompts must explicitly instruct the LLM to carefully verify against the provided problem statement before claiming code is incorrect.
