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
│  TIER 1: episodic_log (Rolling — 5,000-row EMA cap)             │
│  "User read: Virtual Memory — Galvin Ch9 [Mon 3PM]"             │
│  "User coded: twoSum in Python [Mon 4PM]"                       │
│  "User researched: sliding window technique [Mon 5PM]"          │
├─────────────────────────────────────────────────────────────────┤
│  TIER 0: In-RAM (Hot — Instant access, 30-min flush)            │
│  EMA priority_map: {"code.exe": 0.89, "chrome.exe": 0.73}      │
│  Markov transitions: {"code|chrome|Morning" → "chrome": 14}     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 1. The Database (SQLite + sqlite-vec)

We use a local SQLite database enhanced with `sqlite-vec` for brute-force cosine similarity searches on embeddings.

### Core Tables

1. **`episodic_log`** (The Short-Term Memory)
   Stores every chunk of text read from the screen.
   - `id`: Primary key.
   - `timestamp`: Unix time.
   - `app_name`: "VS Code", "Chrome", etc.
   - `raw_text`: The actual text on the screen.
   - `vector_embedding`: 384-dimensional float array (from multilingual-e5).
   - `importance_score`: Float (0.0 to 1.0) calculated by the AI (see *Importance-Based Pruning* below).

2. **`core_persona`** (The Long-Term Memory)
   Stores the structured JSON facts extracted during the nightly routine.
   - `id`: Primary key.
   - `fact_json`: The structured JSON (e.g. `{"topics_studied": ["Graph Algorithms"]}`).
   - `timestamp`: When it was extracted.

3. **`transition_matrix`** (The Markov Chain)
   Stores O(1) app switching behavior for prediction.
   - `state_key`: e.g. "VS Code|Chrome|Night"
   - `next_app`: e.g. "Terminal"
   - `count`: How many times this happened.

4. **`priority_map`** (The Exponential Moving Average)
   Stores the importance of an app based on frequency of use.
   - `app_name`: "VS Code"
   - `ema_score`: Float

5. **`knowledge_library`** (The Concept Vault / Continual Learning)
   Stores high-value architectural answers and explanations provided by the Gemini API, so Gemma can use them as reference in the future without calling the API again.
   - `id`: Primary key.
   - `topic_vector`: 384-dimensional float array for RAG lookup.
   - `concept_summary`: The detailed markdown explanation.
   - `source`: e.g. "Gemini API".
   - `timestamp`: Unix time.

---

## 2. Importance-Based Memory Pruning

Instead of simply deleting the oldest memories (which might delete crucial study notes), Jugnu uses an **Importance-Based System**.

### The Scoring Flow
1. C++ reads the screen.
2. The Python background service evaluates the text and assigns an `importance_score` (0.0 to 1.0).
   - *0.1 = Scrolling YouTube.*
   - *0.9 = Debugging C++ code.*
3. C++ stores the row in `episodic_log`.

### The Nightly Decay
Every night at 2 AM, the C++ engine runs an `UPDATE` query that decays the `importance_score` of all rows by 5% (e.g. `importance_score = importance_score * 0.95`).

### The Pruning Rule
Rows are only deleted from `episodic_log` if their `importance_score` drops below **0.2**. 
- A highly important coding session (starting at 0.9) will survive for weeks.
- A mindless scrolling session (starting at 0.15) will be deleted on the very first night.

---

## 3. Structured Nightly Extraction

While the `episodic_log` handles raw screen text, the `core_persona` handles abstract knowledge. 

Every night, Jugnu gathers all the high-importance episodic logs from the day and sends them to the local Gemma model. The model is instructed (see `prompts_design.md`) to output a strict JSON structure containing the essence of the day:

```json
{
  "key_topics_studied": ["Algorithms", "C++ Vectors"],
  "struggles_or_bugs": ["Memory leak in WebView2"],
  "projects_advanced": ["Jugnu AI Desktop App"],
  "long_term_facts_learned": ["User prefers typing in dark mode"]
}
```
This JSON is permanently saved in the `core_persona` table, ensuring Jugnu never forgets the "big picture" of your life.

---

## 4. The 30-Minute Flush Strategy

To keep the system running at 0% CPU, the Markov Chain and EMA maps are updated entirely in RAM (using C++ `std::unordered_map`). 
To prevent data loss if the laptop crashes, a background C++ thread wakes up every 30 minutes, takes a mutex lock, and flushes the RAM maps into the SQLite database.
