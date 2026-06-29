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
