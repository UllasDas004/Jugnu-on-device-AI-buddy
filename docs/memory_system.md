# Memory System Design

## Overview

The memory system is a three-tier architecture:

```
┌─────────────────────────────────────────────────────────────────┐
│  TIER 2: core_persona (Permanent — Never evicted)               │
│  "User is preparing for FAANG placements"                       │
│  "User codes in Python and C++"                                 │
│  "User is in 3rd year Computer Science"                         │
├─────────────────────────────────────────────────────────────────┤
│  TIER 1: episodic_memories (Rolling — 5,000-row EMA cap)        │
│  "User read: Virtual Memory — Galvin Ch9 [Mon 3PM]"             │
│  "User coded: twoSum in Python [Mon 4PM]"                       │
│  "User researched: sliding window technique [Mon 5PM]"          │
│  + vec_episodic (VIRTUAL TABLE — 384-dim float vectors)         │
├─────────────────────────────────────────────────────────────────┤
│  TIER 0: In-RAM (Hot — Instant access, 30-min flush)            │
│  EMA priority_map: {"code.exe": 0.89, "chrome.exe": 0.73}      │
│  Markov transitions: {"code|chrome|Morning" -> "chrome": 14}    │
└─────────────────────────────────────────────────────────────────┘
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

2. **`vec_episodic`** (The Vector Index — VIRTUAL TABLE)
   Created with `sqlite-vec`'s `vec0` engine. Stores 384-dimensional float arrays
   corresponding to `episodic_memories` rows. Shares the same `rowid`.
   - `embedding float[384]`: Binary blob of IEEE 754 single-precision floats.
   - Searched using `WHERE embedding MATCH ? AND k = ?` KNN syntax.

3. **`markov_edges`** (The Markov Chain)
   Stores O(1) app switching behaviour for prediction.
   - `source_app TEXT`, `target_app TEXT`, `transition_count INTEGER`

4. **`app_paths`** (The RAM Prefetcher Vault)
   Stores the absolute path to each process's executable for RAM prefetching.
   - `process_name TEXT PRIMARY KEY`, `absolute_path TEXT NOT NULL`

---

## 2. The Phase 2 RAG Write Pipeline (Implemented)

### Why Python Writes Directly to SQLite (The Boundary Decision)

When choosing how to store vectors, we had two options:

| Option | Method | Problem |
|---|---|---|
| A | Python sends vector over Named Pipe to C++ | Pushes 1.5KB binary blobs over a text protocol. Messy and fragile. |
| B | Python writes directly to SQLite | Clean. SQLite WAL handles concurrent C++ + Python access safely. |

**We chose Option B.** SQLite's WAL (Write-Ahead Logging) mode allows Python and C++
to hold simultaneous connections without corruption. Python connects with `timeout=5.0`
so it waits safely if C++ is mid-transaction.

### The Full Event Flow

```
C++ detects CLIPBOARD copy, FILE_SAVED, or runs OCR_SCREEN via WinRT GPU
    | JSON over Named Pipe (lightweight text)
ipc_client.py receives the event
    | spawns a daemon Thread so the pipe never blocks
embedder.save_memory(app, title, text) runs in background
    | runs e5-small ONNX on the text (CPU inference, ~20ms)
float[384] vector produced
    | packed into raw bytes via struct.pack('<384f')
BEGIN TRANSACTION
    INSERT into episodic_memories -> gets rowid
    INSERT into vec_episodic at same rowid
COMMIT
    | memory is now permanently searchable
```

### The Two-Table Vector Design

The vector and the metadata are stored in separate tables on purpose:

- `episodic_memories` — stores human-readable text for the nightly Gemma extraction job.
- `vec_episodic` — stores binary vectors for fast cosine KNN lookup.
- They are linked by sharing the same `rowid`.

When C++ searches for context, it JOINs both tables:
```sql
SELECT m.text_content
FROM vec_episodic v
INNER JOIN episodic_memories m ON v.rowid = m.id
WHERE v.embedding MATCH ? AND k = 5
ORDER BY distance ASC;
```

### e5-small Prefix Strategy (Asymmetric Search)

The e5-small-v2 model uses **asymmetric** search:
- Text being **stored**: prefix with `"passage: "`.
- Text being **queried**: prefix with `"query: "`.

Using the wrong prefix makes cosine similarity return garbage. We enforce this:

```python
# Storage (in Embedder.save_memory):
prefixed = f"passage: {text}"

# Search (in Embedder.semantic_search):
query_prefixed = f"query: {query_text}"
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
