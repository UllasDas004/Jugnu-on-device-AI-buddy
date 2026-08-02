"""
practice_mode.py — Self-contained module for Jugnu's Practice Mode.

Everything practice-mode specific lives here:
  - Code bag construction + approach detection (includes the Ollama call)
  - State classification and hint type selection  (pure Python rules)
  - Session DB management (get/create/update/mark_solved)
  - Hint logging and feedback logging
  - Hint history retrieval
  - Hint generation (Ollama call)

Why a separate module?
  AIEngine  — general AI tasks only (answer_with_context, generate_search_query, etc.)
  FlushWorker — UIA capture and OCR flushing only
  practice_mode — the complete practice session lifecycle
"""
import datetime
import difflib
import re
import sqlite3
from pathlib import Path

import ollama

# ── Constants ─────────────────────────────────────────────────────────────────

# Resolve DB path once at import, relative to the project root (one level up from inference/).
DB_PATH = Path(__file__).resolve().parents[1] / "jugnu.db"

_MODEL_NAME                    = "gemma4:e2b"
_APPROACH_CONFIDENCE_THRESHOLD = 0.6

_FALLBACK_HINTS = {
    "What's your current thinking on the approach?",
    "You seem stuck — what state are you trying to track in this problem?",
}

_CYAN   = "\033[1;36m"
_GREEN  = "\033[1;32m"
_YELLOW = "\033[1;33m"
_RED    = "\033[1;31m"
_RESET  = "\033[0m"

# ── Bag-of-Keywords Construction ──────────────────────────────────────────────

def _construct_bag(code: str) -> str:
    """
    Lightweight keyword bag for approach heuristics.
    Returns a space-separated uppercase string of detected constructs.
    (Replace with tree-sitter later if needed.)
    """
    lowered = code.lower()
    keywords: list[str] = []

    # Control flow
    if re.search(r'\bfor\b',       lowered): keywords.append("FOR")
    if re.search(r'\bwhile\b',     lowered): keywords.append("WHILE")
    if re.search(r'\bdo\s+\{',     lowered): keywords.append("DO_WHILE")
    if re.search(r'\brecurs\b',    lowered): keywords.append("RECURSIVE")

    # Containers
    if re.search(r'\bunordered_map\b', lowered) or re.search(r'\bhash_map\b', lowered):
        keywords.append("HASHMAP")
    if re.search(r'\bmap\b',           lowered): keywords.append("MAP")
    if re.search(r'\bvector\b',        lowered): keywords.append("VECTOR")
    if re.search(r'\bset\b',           lowered): keywords.append("SET")
    if re.search(r'\bstack\b',         lowered): keywords.append("STACK")
    if re.search(r'\bqueue\b',         lowered): keywords.append("QUEUE")
    if re.search(r'\bpriority_queue\b',lowered): keywords.append("PQ")
    if re.search(r'\bdeque\b',         lowered): keywords.append("DEQUE")

    # Paradigms
    if re.search(r'\bdfs\b', lowered) or re.search(r'\bdepth.*first\b', lowered):
        keywords.append("DFS")
    if re.search(r'\bbfs\b', lowered) or re.search(r'\bbreadth.*first\b', lowered):
        keywords.append("BFS")
    if re.search(r'\bdynamic.*programming\b', lowered) or re.search(r'\bdp\b', lowered):
        keywords.append("DP")
    if re.search(r'\bmemo\b',          lowered): keywords.append("MEMOIZATION")
    if re.search(r'\bgreedy\b',        lowered): keywords.append("GREEDY")
    if re.search(r'\btwo\s*pointer\b', lowered) or re.search(r'\bSliding\s*Window\b', lowered):
        keywords.append("TWO_POINTER")
    if re.search(r'\bbinary_search\b', lowered) or re.search(r'\blower_bound\b', lowered):
        keywords.append("BINARY_SEARCH")
    if re.search(r'\bheap\b', lowered) or re.search(r'\bpriority_queue\b', lowered):
        keywords.append("HEAP")
    if re.search(r'\bunion.*find\b', lowered) or re.search(r'\bdisjoint\b', lowered):
        keywords.append("UNION_FIND")
    if re.search(r'\btrie\b',          lowered): keywords.append("TRIE")
    if re.search(r'\bsegment_tree\b',  lowered) or re.search(r'\bfenwick\b', lowered):
        keywords.append("SEG_TREE")

    # Misc
    if re.search(r'\bsort\b',   lowered): keywords.append("SORT")
    if re.search(r'\bbinary\b', lowered): keywords.append("BINARY")
    if re.search(r'\bmod\b',    lowered): keywords.append("MOD")

    return " ".join(sorted(set(keywords))) if keywords else "NONE"


# ── State Classifier (lightweight gate only) ──────────────────────────────────

def classify_state(
    current_code: str,
    last_code_snapshot: str | None,
) -> str:
    """
    Lightweight classifier. Only used to gate "not enough code yet".
    Returns: READING | STUCK | PROGRESSING
    """
    meaningful_lines = [
        ln for ln in (current_code or "").splitlines()
        if ln.strip()
        and not ln.strip().startswith("//")
        and not ln.strip().startswith("#")
    ]
    if len(meaningful_lines) < 5:
        return "READING"

    if last_code_snapshot:
        ratio = difflib.SequenceMatcher(
            None, last_code_snapshot, current_code, autojunk=False
        ).ratio()
        # > 90% same = stuck (no meaningful progress since last hint)
        return "STUCK" if ratio > 0.90 else "PROGRESSING"

    return "PROGRESSING"  # First time: treat as progressing, let Gemma evaluate


# ── Gemma-Driven Hint Generator ──────────────────────────────────────────────

def generate_practice_hint(
    problem_content:     str,
    problem_notes:       str,
    current_code:        str,
    hint_history:        list[str],
    last_feedback:       int | None,
    user_state:          str = "STUCK",
) -> tuple[str, str, str, int]:
    """
    Single Gemma call to evaluate the code and generate the actual hint text.
    Returns (hint_type, hint_text, approach, is_solved).
    """
    problem_content = (problem_content or "")
    problem_notes   = (problem_notes   or "")
    current_code    = (current_code    or "")

    history_section = ""
    if hint_history:
        numbered = "\n".join(f'[{i+1}] "{h}"' for i, h in enumerate(hint_history[-3:]))
        history_section = f"\nPREVIOUS HINTS (do NOT repeat — build forward):\n{numbered}\n"
    
    feedback_str = ""
    if last_feedback is not None:
        fb_text = "helpful" if last_feedback == 1 else "not helpful"
        feedback_str = f"\nThe user found your last hint {fb_text}. Adjust your approach accordingly.\n"

    prompt = (
        f"You are Jugnu, a senior competitive programming coach running a mock interview.\n"
        f"Developer state: {user_state}. They need a HINT — not a solution.\n\n"
        f"PROBLEM STATEMENT:\n{problem_content}\n\n"
        f"ALGORITHMIC INSIGHT (FOR YOUR EYES ONLY — DO NOT REVEAL DIRECTLY):\n"
        f"{problem_notes if problem_notes else 'Not available yet.'}\n\n"
        f"DEVELOPER'S CURRENT CODE:\n```\n{current_code}\n```\n"
        f"{history_section}"
        f"{feedback_str}\n"
        f"ABSOLUTE RULES:\n"
        f"1. DO NOT write any code, pseudocode, or syntax in the hint.\n"
        f"2. Frame your hint as a Socratic question or observation to guide them (1-2 sentences max).\n"
        f"3. Tailor the hint specifically to their current code and progress. If they are far off track, gently nudge them toward the right approach. If they are close, point out the specific logic flaw.\n"
        f"4. Output EXACTLY in this format:\n"
        f"APPROACH: <Describe their current approach in 2-5 words>\n"
        f"IS_SOLVED: <1 if their code completely and correctly solves the problem, otherwise 0>\n"
        f"TYPE: <A 1-2 word category for this hint (e.g. Conceptual, Logic Flaw, Edge Case, etc)>\n"
        f"HINT: <Your 1-2 sentence hint ending with a question>"
    )

    hint_type = "CONCEPTUAL"
    hint_text = "What's your current thinking on the approach?"
    approach = "unknown"
    is_solved = 0
    
    print(f"\n{_YELLOW}=== EXACT GEMMA PROMPT ==={_RESET}")
    print(f"{prompt}")
    print(f"{_YELLOW}=========================={_RESET}\n")
    
    try:
        response = ollama.chat(
            model=_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            think=False,
            options={"num_ctx": 4096, "num_predict": 150, "temperature": 0.2, "flash_attn": False},
        )
        raw = (
            (response.message.content or "").strip()
            if hasattr(response, "message") and response.message else ""
        )
        
        # Parse output
        lines = raw.splitlines()
        for i, line in enumerate(lines):
            line = line.strip()
            if line.upper().startswith("APPROACH:"):
                approach = line.split(":", 1)[1].strip()
            elif line.upper().startswith("IS_SOLVED:"):
                val = line.split(":", 1)[1].strip()
                is_solved = 1 if val == "1" else 0
            elif line.upper().startswith("TYPE:"):
                extracted = line.split(":", 1)[1].strip().upper()
                extracted = "".join(c for c in extracted if c.isalpha() or c == '_')
                if extracted: hint_type = extracted
            elif line.upper().startswith("HINT:"):
                hint_text = line.split(":", 1)[1].strip()
                if i + 1 < len(lines):
                    hint_text += " " + " ".join(l.strip() for l in lines[i+1:] if l.strip())
                break
                
        if not hint_text or hint_text == "What's your current thinking on the approach?":
            if raw and not any(raw.upper().startswith(prefix) for prefix in ["TYPE:", "APPROACH:", "IS_SOLVED:"]):
                hint_text = raw
                
        # --- NEW LOGS FOR GEMMA OUTPUTS ---
        print(f"\n{_CYAN}=== GEMMA PRACTICE MODE OUTPUT ==={_RESET}")
        print(f"{_CYAN}APPROACH :{_RESET} {approach}")
        print(f"{_CYAN}IS_SOLVED:{_RESET} {is_solved}")
        print(f"{_CYAN}TYPE     :{_RESET} {hint_type}")
        print(f"{_CYAN}HINT     :{_RESET} {hint_text}")
        print(f"{_CYAN}=================================={_RESET}\n")
                
    except Exception as e:
        print(f"{_RED}[Practice] generate_practice_hint error: {e}{_RESET}")

    return hint_type, hint_text, approach, is_solved


def get_last_hint_id(session_id: int) -> int | None:
    """Returns the ID of the most recent hint for a session."""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM practice_hints WHERE session_id = ? ORDER BY timestamp DESC LIMIT 1",
            (session_id,)
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        print(f"{_RED}[Practice] get_last_hint_id error: {e}{_RESET}")
        return None
    finally:
        if conn:
            conn.close()


# ── Session DB Helpers ────────────────────────────────────────────────────────

_SESSION_CACHE = {}

def get_or_create_session(slug: str, platform: str) -> dict | None:
    """
    Returns the active practice session for this problem from cache or DB,
    or creates a fresh row. Returns None on error.
    """
    global _SESSION_CACHE
    if slug in _SESSION_CACHE:
        return _SESSION_CACHE[slug]

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Always fetch the most recent session for this slug, regardless of age or solved status
        cur.execute(
            """
            SELECT id, problem_slug, platform,
                   code_snapshot, detected_approach, approach_confidence,
                   is_solved, user_state, last_hint_type, hint_type_history
            FROM practice_sessions
            WHERE problem_slug = ?
            ORDER BY last_seen DESC
            LIMIT 1
            """,
            (slug,),
        )
        row = cur.fetchone()

        if row:
            session = dict(row)
            print(f"{_CYAN}[Practice] Resumed session for '{slug}' "
                  f"(state={session.get('user_state','?')}){_RESET}")
            _SESSION_CACHE[slug] = session
            return session

        # No active session — create a fresh one
        now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        cur.execute(
            """
            INSERT INTO practice_sessions
                (problem_slug, platform, session_start, last_seen, is_solved)
            VALUES (?, ?, ?, ?, 0)
            """,
            (slug, platform, now, now),
        )
        conn.commit()
        new_id = cur.lastrowid
        print(f"{_GREEN}[Practice] New session #{new_id} created for '{slug}'{_RESET}")
        
        session = {
            "id": new_id, "problem_slug": slug, "platform": platform,
            "code_snapshot": None, "detected_approach": None,
            "approach_confidence": None, "is_solved": 0,
            "user_state": "READING", "last_hint_type": None,
            "hint_type_history": "[]",
        }
        _SESSION_CACHE[slug] = session
        return session
    except Exception as e:
        print(f"{_RED}[Practice] get_or_create_session error: {e}{_RESET}")
        return None
    finally:
        if conn:
            conn.close()

def flush_session_to_db(slug: str) -> None:
    """Writes the in-memory cached session state back to the database."""
    global _SESSION_CACHE
    session = _SESSION_CACHE.get(slug)
    if not session:
        return
        
    set_clauses = []
    params = []
    
    for field in ["last_hint_type", "hint_type_history", "code_snapshot", 
                  "detected_approach", "approach_confidence", "is_solved", "user_state"]:
        set_clauses.append(f"{field} = ?")
        params.append(session.get(field))
        
    set_clauses.append("last_seen = datetime('now')")
    params.append(session["id"])
    
    sql = f"""
        UPDATE practice_sessions
        SET {", ".join(set_clauses)}
        WHERE id = ?
    """
    
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.execute(sql, tuple(params))
        conn.commit()
    except Exception as e:
        print(f"{_RED}[Practice] flush_session_to_db error: {e}{_RESET}")
    finally:
        if conn:
            conn.close()

def update_session(
    slug: str,
    platform: str,
    *,
    last_hint_type: str | None = None,
    hint_type_history: str | None = None,
    code_snapshot: str | None = None,
    detected_approach: str | None = None,
    approach_confidence: float | None = None,
    is_solved: int | None = None,
    user_state: str | None = None,
) -> None:
    """
    Updates the session in the in-memory cache ONLY. 
    Call flush_session_to_db to persist to disk.
    """
    session = get_or_create_session(slug, platform)
    if not session:
        return

    if last_hint_type is not None:      session["last_hint_type"] = last_hint_type
    if hint_type_history is not None:   session["hint_type_history"] = hint_type_history
    if code_snapshot is not None:       session["code_snapshot"] = code_snapshot
    if detected_approach is not None:   session["detected_approach"] = detected_approach
    if approach_confidence is not None: session["approach_confidence"] = approach_confidence
    if is_solved is not None:           session["is_solved"] = is_solved
    if user_state is not None:          session["user_state"] = user_state


def mark_session_solved(session_id: int) -> None:
    """Marks a practice session row as solved by its primary key."""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.execute(
            "UPDATE practice_sessions SET is_solved = 1, last_seen = datetime('now') WHERE id = ?",
            (session_id,),
        )
        conn.commit()
    except Exception as e:
        print(f"{_RED}[Practice] mark_session_solved error: {e}{_RESET}")
    finally:
        if conn:
            conn.close()





# ── Hint Logging ──────────────────────────────────────────────────────────────

def log_hint(
    session_id: int,
    hint_type: str,
    hint_text: str,
    user_state: str,
    code_snapshot: str,
    approach: str,
) -> int | None:
    """
    Inserts a hint record into practice_hints.
    Returns the new row id — save it, it's needed for log_feedback().
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO practice_hints
                (session_id, hint_type, hint_text, user_state, code_snapshot, approach_at_hint)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                hint_type,
                hint_text[:2000],
                user_state,
                (code_snapshot or "")[:3000],
                approach or "unknown",
            ),
        )
        conn.commit()
        hint_id = cur.lastrowid
        conn.close()
        return hint_id
    except Exception as e:
        print(f"{_RED}[Practice] log_hint error: {e}{_RESET}")
        return None


def log_feedback(
    hint_id: int,
    *,
    user_feedback: int | None = None,
    implicit_code_changed: int | None = None,
    test_outcome_after: str | None = None,
) -> None:
    """
    Updates a practice_hints row with one or more feedback signals.
    Called by notification.py (explicit button press) and FlushWorker (submission result).
    """
    set_parts: list[str] = []
    vals: list = []

    if user_feedback is not None:
        set_parts.append("user_feedback = ?")
        vals.append(user_feedback)
    if implicit_code_changed is not None:
        set_parts.append("implicit_code_changed = ?")
        vals.append(implicit_code_changed)
    if test_outcome_after is not None:
        set_parts.append("test_outcome_after = ?")
        vals.append(test_outcome_after)

    if not set_parts:
        return

    vals.append(hint_id)
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.execute(
            f"UPDATE practice_hints SET {', '.join(set_parts)} WHERE id = ?",
            tuple(vals),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"{_RED}[Practice] log_feedback error: {e}{_RESET}")


def get_last_hints(session_id: int, n: int = 3) -> list[str]:
    """Returns the last n hint texts for a session, oldest first."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT hint_text FROM practice_hints
            WHERE session_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (session_id, n),
        )
        rows = cur.fetchall()
        conn.close()
        return [r[0] for r in reversed(rows)]
    except Exception as e:
        print(f"{_RED}[Practice] get_last_hints error: {e}{_RESET}")
        return []


def get_last_hint_id(session_id: int) -> int | None:
    """Returns the id of the most recent hint for this session."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM practice_hints WHERE session_id = ? ORDER BY timestamp DESC LIMIT 1",
            (session_id,),
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None



def generate_efficiency_review(
    problem_content: str,
    current_code: str,
    problem_slug: str,
    platform: str,
) -> str:
    """
    Generates a brief code review for a completely correct solution.
    Focuses on time/space complexity and potential optimizations.
    """
    problem_content = (problem_content or "")[:1500]
    current_code    = (current_code    or "")[:2500]
    
    prompt = (
        f"You are Jugnu, a senior competitive programming coach.\n"
        f"The developer has just successfully solved the problem '{problem_slug}'.\n\n"
        f"PROBLEM STATEMENT:\n{problem_content}\n\n"
        f"DEVELOPER'S CORRECT CODE:\n```\n{current_code}\n```\n\n"
        f"TASK:\n"
        f"Provide a brief, encouraging code review.\n"
        f"1. State the time and space complexity of their solution.\n"
        f"2. Point out any redundant operations or ways to make it more elegant/optimal.\n"
        f"3. If it's already optimal, praise them and explain why it's the best approach.\n"
        f"Keep it concise, max 3-4 sentences. Do NOT write full alternative solutions."
    )
    
    try:
        response = ollama.chat(
            model=_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            think=False,
            options={"num_ctx": 8192, "num_predict": 300, "temperature": 0.3, "flash_attn": False},
        )
        if hasattr(response, "message") and response.message:
            return (response.message.content or "").strip()
    except Exception as e:
        print(f"{_RED}[Practice] generate_efficiency_review error: {e}{_RESET}")
        
    return "Great job solving the problem! Your solution is correct. Consider reviewing the time and space complexity to see if it can be optimized further."
