# 🚀 Jugnu — Advanced Windows Features Roadmap

Because Jugnu is built as a native C++ Windows application, it has access to low-level OS APIs that standard web-based AI tools cannot touch. This allows Jugnu to act proactively rather than just reactively.

These features are scheduled for **Phase 3** of development.

---

## 1. Proactive Buddy Cards & The Stuck Timer
**The Problem:** The user shouldn't have to constantly open the UI to ask for help if they are visibly struggling.
**The Solution:** Jugnu detects when you are stuck and slides a small, non-intrusive hint card into the corner of your screen.

**Backend Implementation (Anchor Vector Classification):**
To avoid hardcoding fragile C++ string checks (like `if text.contains("Exception")`), Jugnu uses Semantic Vectors:
1. On startup, Python generates a 384-dim embedding for an "Error Anchor" (e.g., *"Stack trace, compiler exception, syntax error, crash"*).
2. As C++ reads the screen, it receives the embedding for the current text.
3. C++ runs a lightning-fast dot product (Cosine Similarity) between the screen vector and the Error Anchor.
4. If similarity > 0.85 and `GetLastInputInfo()` shows no typing for 3 minutes → **Trigger STUCK_STATE**.
5. Python triggers a frameless `pywebview` Notification Card in the corner, and Gemma (via Ollama) generates a 1-sentence hint.

---

## 2. Clipboard Interception (Smart Paste)
**The Problem:** Copy-pasting massive stack traces into an AI chat is tedious.
**The Solution:** Jugnu intercepts massive code/error copies and pre-loads them.

**Backend Implementation (C++):**
- Uses `AddClipboardFormatListener()` to receive `WM_CLIPBOARDUPDATE` messages.
- If the copied text is > 5 lines and looks like code or an error log, C++ silently passes it to Python.
- When the user presses `Ctrl+Space`, the Jugnu UI is already waiting with the message: *"I see you copied an Exception. Want me to debug it?"*

---

## 3. The Flow State Enforcer (Pomodoro 2.0)
**The Problem:** It is too easy to get distracted by YouTube or Twitter during a heavy coding session.
**The Solution:** Jugnu tracks your Flow State and actively intervenes if you break focus.

**Backend Implementation (C++):**
- Uses the existing `EVENT_SYSTEM_FOREGROUND` hook.
- If the user stays in `VS Code` for > 45 minutes, they are in a "Flow State".
- If the next app switch is to a known distraction domain (e.g. `twitter.com` in Chrome), C++ triggers a Buddy Card popup: *"You've been in flow for 45 mins! Take a real break instead of scrolling."*

---

## 4. File System Ghost-Writer
**The Problem:** Developers hate writing documentation, and AI tools lack full-file context if you only give them snippets.
**The Solution:** Jugnu silently reads your code as you save it and generates documentation in the background.

**Backend Implementation (C++):**
- Uses `ReadDirectoryChangesW()` on the user's defined `coding_folder` (set during onboarding).
- When a `FILE_ACTION_MODIFIED` event triggers (i.e., you hit `Ctrl+S`), C++ reads the file.
- It passes the file to Python, which generates a summary of the logic and saves it to the `core_persona` database. Jugnu always knows what your code does.

---

## 5. The Alt-Tab Predictor (Context Pre-loading)
**The Problem:** LLM inference takes 1-2 seconds, which feels slow for a real-time assistant.
**The Solution:** Predict when the user is about to ask a question and preload the context.

**Backend Implementation (C++ & Python):**
- The C++ Second-Order Markov Chain predicts the next app switch.
- If the prediction shows a high probability that the user is moving from `VS Code` to `Jugnu Chat` (because they usually do this when stuck), C++ proactively passes the VS Code text to Python *before* the user even hits the hotkey.
- Python processes the context into VRAM early, resulting in a near-instant 0.1s response when the user finally asks their question.

---

## 6. The "Oops" Rollback (Time Machine)
**The Problem:** Users accidentally delete large blocks of code and lose their `Ctrl+Z` undo history when closing the editor.
**The Solution:** Jugnu acts as a passive version-control system for everything on the screen.

**Backend Implementation (C++):**
- Because C++ constantly embeds and saves the screen state to the `episodic_log` with timestamps, the user can literally ask: *"What did my `solution.cpp` look like 2 hours ago?"*
- Python generates a SQL query to fetch the exact log entry from that timestamp, effectively recovering lost code.

---

## 7. Drag & Drop Local RAG (The Offline Search Engine)
**The Problem:** Jugnu only knows what is actively on the screen. It can't read a closed 50-page PDF textbook.
**The Solution:** Allow the user to drag-and-drop massive files directly into the UI.

**Backend Implementation (C++):**
- The PyWebView window accepts HTML5 drag-and-drop events.
- C++ parses the file (PDF, TXT, or ZIP of a codebase), chunks the text, and passes it to the Python ONNX embedder.
- The vectors are saved into a new `document_vault` SQLite table, instantly turning Jugnu into a local search engine for entire textbooks and codebases.

---

## 8. Audio Fading (Post-Launch Polish)
**The Problem:** Standard popups are easy to ignore or can be jarring if the user is listening to music.
**The Solution:** Jugnu smoothly fades the user's system audio when asking a question.

**Backend Implementation (C++):**
- Uses the Windows `IAudioEndpointVolume` API.
- When the Stuck Timer triggers a Buddy Card, C++ smoothly lowers the system volume by 20% to subtly grab the user's attention. Once the user clicks or dismisses the card, the volume fades back up.

---

## 9. Predictive App Pre-caching (OS-Level Optimization)
**The Problem:** Heavy IDEs (Visual Studio, Android Studio) take several seconds to load from an SSD.
**The Solution:** Use Jugnu's Markov Chain to physically page the app into RAM before the user even clicks it.

**Backend Implementation (C++):**
- Windows has built-in SuperFetch, but it is not context-aware. Jugnu's Second-Order Markov Chain knows that when you are in `Spotify` at 9 AM, your next click is 95% likely to be `VS Code`.
- When the prediction hits >90% probability, a background C++ thread uses `CreateFile` with `FILE_FLAG_SEQUENTIAL_SCAN` to silently read the `.exe` and its main `.dll` files from the disk.
- This forces the Windows OS to load the app's binaries into the RAM Page Cache.
- When you double-click the app 5 seconds later, it launches near-instantly from RAM instead of reading from the SSD.


> **Milestone Update**: Phase 1 (Core IPC + Interim UI) is integrated! The C++ Tracker now dynamically invokes the Python inference engine to display glassmorphic PyWebView notifications without deadlocks.
