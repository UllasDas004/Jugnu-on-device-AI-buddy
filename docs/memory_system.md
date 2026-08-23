# Memory System Design

## Overview

The memory system is a three-tier architecture:

```
+-----------------------------------------------------------------------+
|  TIER 2: knowledge_docs + vec_knowledge  (Structured Long-Term Memory)|
|  "Topic: Two Sum — Sliding Window | Tags: cp, leetcode, two-pointers" |
|  Written to directly by C++ ScreenReader (Gear 1 tab-switch fast path)|
|  Enriched by Python FlushWorker (TOPIC, TAGS, NOTES + vec embedding)  |
|  Also holds: practice_sessions + practice_hints (CP Practice Mode)    |
+-----------------------------------------------------------------------+
|  TIER 1: episodic_memories + vec_episodic  (Rolling Short-Term Memory)|
|  "User saved: main.cpp with BFS implementation [Mon 3PM]"             |
|  384-dim e5-small-v2 float vectors. 5,000-row EMA cap.               |
+-----------------------------------------------------------------------+
|  TIER 0.5: ocr_buffer  (Staging — C++ OCR fallback only)             |
|  Raw dirty UIA/OCR blobs. Purged after successful FlushWorker sync.   |
|  NOTE: Gear 1 tab-switch now writes DIRECTLY to knowledge_docs,       |
|  bypassing ocr_buffer. This table is only for background OCR fallback.|
+-----------------------------------------------------------------------+
|  TIER 0: In-RAM  (Hot — flushed to SQLite every 30 min)              |
|  EMA priority_map:   {"code.exe": 0.89, "chrome.exe": 0.73}         |
|  Dynamic Governor:   throttles apps where 0 < EMA < 0.25             |
|  Markov transitions: {"code|chrome|Morning" -> "chrome": 14}         |
|  g_lastCodeBuffer:   live Monaco code from Ghost Clipboard (volatile) |
+-----------------------------------------------------------------------+

Write paths:
  C++ Gear 1 (tab-switch) ---> knowledge_docs (direct, skips ocr_buffer)
                           ---> IPC: UIA_EXTRACTION_SAVED -> Python enriches
  C++ OCR fallback        ---> ocr_buffer -> [FlushWorker 60s] -> knowledge_docs
  C++ Gear 2 (typing)     ---> g_lastCodeBuffer (RAM only, never touches DB)
  Python FlushWorker      ---> process_uia_by_id(row_id) -> vec_knowledge embed
  File Save / Clipboard   ---> episodic_memories + knowledge_docs (via Python)
```

---

## 1. The Database (SQLite + sqlite-vec)

We use a local SQLite database enhanced with `sqlite-vec` for KNN cosine similarity searches on embeddings.

### Core Tables

1. **`episodic_memories`** (The Short-Term Memory)
   Stores every chunk of text captured from clipboard or file saves.
   - `id`: Primary key (AUTOINCREMENT).
   - `app_name`: "VS Code", "Chrome", etc.
   - `window_title`: The full window title at capture time.
   - `text_content`: The raw text (up to 4000 chars for code files).
   - `timestamp`: Auto-set by SQLite.

2. **`ocr_buffer`** (The Staging Area)
   A temporary holding zone for massive, dirty screen text captured by C++.
   - `id`: Primary key.
   - `app_name`: Source of the screenshot.
   - `raw_text`: The uncleaned OCR dump.
   - *Note*: Rows here are aggressively chunked, cleaned by Gemma via `flush_worker.py`, inserted into `episodic_memories`, and then deleted from this table.

2. **`vec_episodic`** (The Vector Index — VIRTUAL TABLE)
   Created with `sqlite-vec`'s `vec0` engine. Stores 384-dimensional float arrays
   corresponding to `episodic_memories` rows. Shares the same `rowid`.
   - `embedding float[384]`: Binary blob of IEEE 754 single-precision floats.
   - Searched using `WHERE embedding MATCH ? AND k = ?` KNN syntax.

4. **`knowledge_docs`** (The OKF Vault)
   Stores highly structured, LLM-synthesized Objective Knowledge Format (OKF) documents extracted from raw code files and screens.
   - `id`: Primary key.
   - `topic`: A short, descriptive title.
   - `summary`: A 1-2 sentence semantic anchor for vector embedding.
   - `content`: Deduplicated explanatory text and concepts.
   - `code_snippet`: Pristine, merged verbatim code blocks.
   - `notes`: Constraints and hints.
   - `tags`: JSON array of semantic tags (e.g., ["Python", "LeetCode"]).
   - `capture_count`: Incremented if the same knowledge is detected again.
   - `source_type`: Origin of data (e.g. `ide`, `browser`, `clipboard`).
   - `full_buffer`: Boolean flag indicating if this is a guaranteed complete file read (like a CTRL+C ghost clipboard grab) vs a partial UI viewport read.

5. **`vec_knowledge`** (The OKF Vector Index — VIRTUAL TABLE)
   The `sqlite-vec` index for `knowledge_docs`. **Crucially**, it only embeds the `topic` and `summary` columns. This prevents the `e5-small-v2` embedding model from being confused by raw code symbols while retaining fast, accurate semantic search.

5. **`markov_edges`** (The Markov Chain)
   Stores O(1) app switching behaviour for prediction.
   - `source_app TEXT`, `target_app TEXT`, `transition_count INTEGER`

6. **`app_paths`** (The RAM Prefetcher Vault)
   Stores the absolute path to each process's executable for RAM prefetching.
   - `process_name TEXT PRIMARY KEY`, `absolute_path TEXT NOT NULL`

---

## 2. The Phase 5 OKF Write Pipeline (Zero-IPC)

### The Great IPC Bottleneck (Why we changed)
Originally, C++ streamed massive OCR text blobs to Python via Named Pipes. This caused the Python background daemon to block for hundreds of milliseconds parsing JSON strings, leading to dropped telemetry events and severe lag. 

### The New Producer-Consumer Architecture
We transitioned to a **Zero-IPC** approach for heavy data:
1. **Producer:** C++ instantly dumps raw, noisy text into the `ocr_buffer` table using native SQLite C APIs (ultra-fast, asynchronous).
2. **Consumer:** Python's `FlushWorker` daemon wakes up every 60 seconds (only on AC power) and chews through the buffer at its own pace without blocking the main event loop.

### The Full Event Flow (Phase 5 Architecture)

```
[C++ Data Sources]
  ├─ UIA / OCR Capture → Native SQLite Insert → [ocr_buffer]
  └─ Ghost Clipboard (CTRL+C) → Bypasses Buffer → Direct DB Insert (Pristine `full_buffer` flag)

[Python FlushWorker (Wakes every 60s)]
  ├─ 1. Prunes rows > 10 mins old (saves GPU)
  ├─ 2. Area-Wise Deduplication: Splits UIA JSON into 'Edit' (Code) and 'Document' (Page). 
  │     Only skips extraction if BOTH are >95% identical to previous frame.
  ├─ 3. UIA JSON Routing:
  │      ├─ 'Edit' (Code) → Bypasses LLM, logged as pristine `verbatim` code.
  │      └─ 'Document' → Gemma extracts TOPIC, TAGS, NOTES (CONTENT is handled directly via Python to save tokens)
  └─ 4. combine_sections(): Pure Python fusion.

[AI Engine]
  └─ generate_summary(): Generates a 1-2 sentence prose anchor

[Embedder]
  ├─ 1. Deterministic Anchor Check: Looks for exact `window_title` or `file_path` matches.
  ├─ 2. Vector Semantic Check: (Fallback) Checks `vec_knowledge` for > 97% cosine similarity.
  ├─ 3. Intelligent Merging:
  │      ├─ "Never Delete, Only Add": difflib union merge preserves user code during scroll-loss.
  │      ├─ OCR-to-UIA Upgrade: Overwrites dirty OCR if pristine UIA data arrives.
  │      └─ Full Buffer Override: If `full_buffer=true` (Clipboard), safe overwrite allowed.
  └─ 4. DELETES row from ocr_buffer (even if merge happened).
```

### e5-small Prefix Strategy (Asymmetric Search)

The e5-small-v2 model uses **asymmetric** search:
- Text being **stored**: prefix with `"passage: "`.
- Text being **queried**: prefix with `"query: "`.

Using the wrong prefix makes cosine similarity return garbage. We enforce this:

```python
# Storage (in Embedder.save_knowledge_doc):
prefixed = f"passage: {topic}. {summary}"

# Search (in Embedder.search_knowledge_docs):
query_prefixed = f"query: {query_text}"
```

---

## 2.6 The Phase 6 Zero-DB IPC Code Hot-Path (Practice Mode)

While Phase 5 eliminated the IPC bottleneck for massive OCR background dumps by using the `ocr_buffer` DB, Practice Mode requires absolute freshness. 

### The Stale DB Race Condition
If the user stops typing, the 3-minute idle timer fires and Python attempts to give a hint. Previously, Python queried SQLite for the latest code. However, because `FlushWorker` only runs every 60s, the DB was often up to 59 seconds out of date, causing the AI to give hints for bugs the user had already fixed.

### The Hot-Path Solution (Gear 2)
We introduced a **Zero-DB IPC Pipeline** exclusively for active code:
1. **Gear 2 RAM Cache (C++):** While the user actively types (accumulating 60s of typing time), the Screen Reader waits for a 5-second pause. When it detects this pause, it fires Ghost Clipboard and saves the code strictly into a volatile `g_lastCodeBuffer` in RAM. It explicitly skips inserting into the SQLite DB to avoid VRAM/CPU churn.
2. **IPC Injection:** When `StuckTimerThread` fires (after 3 minutes of total idle time), it grabs this hot RAM buffer, strictly escapes it (handling quotes, backslashes, tabs, and newlines), and injects it directly into the `USER_IDLE` JSON payload.
3. **Python Bypass:** `ipc_client.py` receives the `ipc_code`. If present, it completely bypasses the SQLite Database retrieval (`state.generate_prompt_context()`), saving 100-200ms of latency and guaranteeing 0ms staleness for the Gemma context window.

---

## 2.5 The Phase 5 OKF Read Pipeline (The RAG Engine)

When the user asks for help (via `jugnu_interact.py`), Jugnu transitions from a passive observer to an active assistant.

### The Full Retrieval Flow

```text
[User Query / Screen Context] 
  └─ AI Engine: `generate_search_query()` creates a highly focused 15-word semantic search string.

[Embedder (search_knowledge_docs)]
  ├─ 1. e5-small-v2 embeds the query with the `"query: "` prefix.
  ├─ 2. sqlite-vec KNN Search (`WHERE embedding MATCH ?`) retrieves top 5 structured OKF docs.
  ├─ 3. Layer 3 Topic Deduplication: Discards docs if their `topic` is 85% identical to an already accepted doc.
  └─ 4. Blended Re-Ranking: Mathematically mutates the cosine distance:
        final_score = distance - (0.12 * recency_bonus) - (0.08 * frequency_bonus)

[State Manager (build_rag_context)]
  ├─ Enforces strict Tiered Token Budgeting to prevent VRAM OOM:
  ├─ Layer 1: Current Screen Context (capped at 3000 chars)
  ├─ Layer 2: Primary Knowledge Doc (Code capped at 2500, Content at 1500)
  └─ Layer 3: Supporting Docs (Content capped at 800 chars)

[AI Engine (answer_with_context)]
  └─ Situation-Aware Prompt Engineering: Reads `capture_count` and current app to inject `REPEATED_STRUGGLE` or `STUCK_ON_OWN_CODE` personas, forcing the LLM to provide hyper-specific bug fixes instead of generic tutorials.
```

---

## 3. sqlite-vec Build Integration (The Static Linking Trap)

`sqlite-vec.h` includes `sqlite3ext.h`, which redefines **every** SQLite function
as a macro pointer (`sqlite3_api->exec`) that only exists in `.dll` extension code.

**The Fix:** Never include `sqlite-vec.h` in application code. Forward-declare only:

```cpp
// db_handler.cpp — instead of #include "sqlite-vec.h"
extern "C" {
    int sqlite3_vec_init(sqlite3 *db, char **pzErrMsg, const void *pApi);
}

// Register before sqlite3_open_v2:
sqlite3_auto_extension((void(*)(void))sqlite3_vec_init);
```

**MSVC Migration Note:** During Phase 3, we migrated to MSVC (`cl.exe`) to unlock native WinRT OCR. MSVC requires the `sqlite3.c` amalgamation to be explicitly compiled as C code, not C++, otherwise `sqlite3_vec_init` will trigger undefined reference linker errors due to C++ name mangling.

---

## 4. Importance-Based Memory Pruning (Planned Phase 3)

Instead of deleting oldest memories, Jugnu will use **Importance-Based Pruning**.

### The Nightly Decay
Every night at 2 AM, a score decays by 5%: `importance_score = importance_score * 0.95`

### The Pruning Rule
Rows deleted only if `importance_score` drops below 0.2.
- A coding session (score 0.9) survives for weeks.
- A YouTube scroll (score 0.15) is deleted the next morning.

---

## 5. The 30-Minute Flush Strategy (flush_worker.cpp — Next in Phase 2)

The Markov Chain and EMA maps live in C++ RAM (`std::unordered_map`). A background
thread wakes every 30 minutes, takes a mutex lock, and calls `FlushMarkovEdges()`
to persist them to SQLite. On next boot, `loadPriorityMap()` and `loadTransitionMatrix()`
restore the full learned state. Maximum data loss on a crash = 30 minutes.

---

## 6. The Dynamic Process Priority Governor

Instead of relying on hardcoded arrays of "distracting" apps (like Spotify, Discord, etc.), Jugnu uses the hot **EMA `priority_map`** to dynamically manage system resources during Deep Work sessions.

### How it Works:
When Jugnu detects a transition into a "Deep Work" state (high EMA app is foregrounded and the system identifies coding/studying intent), the `MemoryManager::ThrottleDistractors()` function runs.

1. It iterates over all running OS processes.
2. It queries the RAM `emaScores` map for each process.
3. If an app has an EMA score that is **strictly greater than 0.0** but **below the `DISTRACTOR_THRESHOLD` (e.g. 0.25)**, Jugnu identifies it as a distractor app (e.g., an app you switch to rarely, but is currently running in the background consuming CPU).
4. Jugnu issues a Win32 `SetPriorityClass` call to downgrade the process to `IDLE_PRIORITY_CLASS`, effectively choking it out from stealing CPU cycles from the compiler/LLM.

**Why `> 0.0`?** 
Unknown system services (like `svchost.exe` or `csrss.exe`) have a score of `0.0` because Jugnu has never seen the user explicitly switch to them. By only throttling apps with a score `> 0.0`, Jugnu ensures it never accidentally chokes critical Windows OS services, creating a perfectly safe, self-training governor.

---

## 7. Practice Mode Database Schema (Phase 8)

Two tables are created by `db_handler.cpp` specifically for the CP Practice Mode. They exist in the same `jugnu.db` file alongside the core memory tables.

### `practice_sessions`
One row = one active problem attempt. A session "expires" if `last_seen > 2h ago` OR `is_solved = 1`.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `problem_slug` | TEXT | e.g., `"two-sum"` |
| `platform` | TEXT | `"leetcode"` or `"codeforces"` |
| `session_start` | DATETIME | When the session began |
| `last_seen` | DATETIME | Updated on every CP event |
| `code_snapshot` | TEXT | Latest code capture |
| `detected_approach` | TEXT | Gemma's approach label (e.g., `"Dynamic Programming"`) |
| `approach_confidence` | REAL | 0.0–1.0 confidence score |
| `is_solved` | INTEGER | 0 or 1 |
| `user_state` | TEXT | `'READING'` or `'CODING'` |
| `last_hint_type` | TEXT | Most recent hint category |
| `hint_type_history` | TEXT | JSON array of all hint types given |

**Constraint:** `UNIQUE(problem_slug, platform)` is required for Python's `ON CONFLICT DO UPDATE` UPSERT to work correctly. Without it, every hint trigger inserts a new row, resetting `hint_level` to 0.

**Index:** `idx_practice_sessions_lookup ON (problem_slug, platform, is_solved, last_seen)` for O(log N) expiry queries.

### `practice_hints`
One row = one hint issued during a session.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `session_id` | INTEGER FK | References `practice_sessions.id` |
| `timestamp` | DATETIME | When hint was generated |
| `hint_type` | TEXT NOT NULL | `'CONCEPTUAL'`, `'LOGIC'`, `'IMPLEMENTATION'` (from Gemma `TYPE:`) |
| `hint_text` | TEXT NOT NULL | The Socratic hint or efficiency review text |
| `user_state` | TEXT | `'READING'` or `'CODING'` at hint time |
| `code_snapshot` | TEXT | Code at the moment of the hint |
| `approach_at_hint` | TEXT | Detected approach when hint was given |
| `user_feedback` | INTEGER | 1 = helpful, 0 = not helpful, NULL = no feedback |
| `implicit_code_changed` | INTEGER | 1 if code changed significantly after hint |
| `test_outcome_after` | TEXT | Future: test result after hint |

**Index:** `idx_practice_hints_session ON (session_id, timestamp DESC)` for O(log N) recent hint retrieval.

### Dashboard Stats Query
When the user clicks the mascot, `_query_dashboard_stats()` in `ipc_client.py` builds a nested JSON structure:
- **Level 1:** All problems ever seen (grouped by slug)
- **Level 2:** All sessions per problem (with hint counts)
- **Level 3:** All hints per session (type, text, feedback, code snapshot)

This is serialized to `dashboard_state.json` and read by `dashboard.html` at spawn time. The dashboard never queries SQLite directly.
