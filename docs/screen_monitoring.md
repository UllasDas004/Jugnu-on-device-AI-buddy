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
This is the same API used by Windows Narrator. It directly asks the active application (like VS Code or Word) to hand over its text tree.
- **Pros:** Instant, flawless accuracy, practically zero CPU overhead.
- **Cons:** Some apps (like Discord or specific web pages) do not expose their text trees properly to UI Automation.

### Tier 2: WGC + OCR (Hardware Accelerated)
**Technology:** Windows Graphics Capture (WGC) + Windows.Media.Ocr.
If Tier 1 fails to find meaningful text, Jugnu takes a high-speed, invisible capture of the window using WGC, and passes it to the built-in Windows 10/11 OCR engine.
- **Pros:** Works on literally anything, including images, PDFs, and custom UIs.
- **Cons:** Slightly heavier on the CPU/GPU than Tier 1.

### Tier 3: The Ignore List
If Jugnu detects massive screen updates with no readable text (e.g., a video game rendering via DirectX, or a full-screen YouTube video), it automatically categorizes the app into Tier 3.
- **Action:** Halts all text extraction for this app to save battery, but continues to log *time spent* in the app to update the EMA priority map.

---

## 3. The Embedding Gate

Once text is successfully extracted via Tier 1 or Tier 2, it isn't automatically saved. 
1. The text is passed to the local Python Inference service.
2. The AI generates an **Importance Score** (0.0 to 1.0).
3. If the score is high enough, the text is converted into a 384-dimensional vector and saved to the SQLite `episodic_log`.

*(Note: See `memory_system.md` for how this Importance Score controls database pruning).*
