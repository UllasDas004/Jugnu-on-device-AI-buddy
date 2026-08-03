import threading
import time
import json
import os
import subprocess
import sys
import tempfile
from embedder import Embedder

# ── Global state ──────────────────────────────────────────────────────

# P0-FIX: Use a Lock instead of a bare bool.
# Multiple USER_IDLE daemon threads can arrive simultaneously. Without a lock,
# both threads see is_generating=False and launch two Gemma calls → VRAM crash.
_gen_lock = threading.Lock()

_COOLDOWN_YES = 20 * 60     # 20 min after successful insight
_COOLDOWN_NO  = 15 * 60     # 15 min after dismissal

_cooldown_until = 0.0
def in_cooldown():
    return time.time() < _cooldown_until
    
# P1-FIX: Never mutate the constants. Update only the timestamp.
def _start_cooldown(seconds: float):
    global _cooldown_until
    _cooldown_until = time.time() + seconds

# ── Core: spawn a new terminal window for the interaction ─────────────

def _spawn_interaction_window(context_summary, sources, situation_type, context_chunks):
    """
    Writes context to a temp file, opens a new PowerShell window running
    jugnu_interact.py, waits for it to finish, reads the result.
    Returns the result dict, or {"action": "decline"} on any failure.
    """
    # Find the inference directory relative to this file
    inference_dir = os.path.dirname(os.path.abspath(__file__))
    interact_script = os.path.join(inference_dir, "jugnu_interact.py")

    # Write context to a temp JSON file
    tmp_dir = tempfile.gettempdir()
    state_file  = os.path.join(tmp_dir, "jugnu_state.json")
    result_file = os.path.join(tmp_dir, "jugnu_result.json")
    done_file   = result_file + ".done"

    # Clean up any leftover files from previous runs
    for f in [state_file, result_file, done_file]:
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass

    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({
            "summary": context_summary,
            "mode":     "practice_hint" if situation_type == "CP_STUCK" else ("practice_solved" if situation_type == "CP_SOLVED" else "general"),
            "hint_text": context_chunks[0] if (situation_type in ("CP_STUCK", "CP_SOLVED") and context_chunks) else "",
            "sources":   sources or [],
            }, f)

    # Build the python command (use uv if available, else plain python)
    python_cmd = f'uv run python "{interact_script}" "{state_file}" "{result_file}"'

    # Spawn a new PowerShell window. -NoExit keeps it open after the script finishes.
    proc = subprocess.Popen(
        ["pwsh", "-NoExit", "-Command", python_cmd],
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )

    # Wait for the interaction script to write the result file
    print("\033[90m[Notification] Waiting for user response in the new window...\033[0m", flush=True)
    timeout = 120  # seconds to wait for user
    start = time.time()
    while not os.path.exists(result_file):
        if time.time() - start > timeout:
            print("\033[90m[Notification] Timed out waiting for user.\033[0m", flush=True)
            
            # P1-FIX: Graceful shutdown first, force-kill only as last resort.
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            return {"action": "decline", "custom_problem": None}
        time.sleep(0.3)

    # Read the decision
    try:
        with open(result_file, "r", encoding="utf-8") as f:
            result = json.load(f)
    except Exception:
        result = {"action": "decline", "custom_problem": None}

    return result, proc, done_file

from ai_engine import AIEngine
from state_manager import StateManager

def trigger_flow(state: StateManager, engine: AIEngine, embedder: Embedder,
                 search_query=None, context_chunks=None,
                 knowledge_docs=None, sources=None,
                 screen_context=None, situation_type="GENERAL",
                 session_id=None, hint_id=None):
    # Tell type checkers these are not None
    assert engine is not None
    assert embedder is not None

    # P0-FIX: non-blocking acquire — if another thread is already generating, bail out.
    if not _gen_lock.acquire(blocking=False):
        print("\033[90m[Notification] Already generating. Skipping duplicate trigger.\033[0m")
        return

    if in_cooldown():
        _gen_lock.release()
        print("\033[90m[Notification] On cooldown. Skipping trigger.\033[0m")
        return

    # Prepare context summary
    if sources is None:
        sources = []
    if context_chunks is None:
        context_chunks = []
    if knowledge_docs is None:
        knowledge_docs = []

    summary = state.get_context_summary()

    spawn_result = _spawn_interaction_window(summary, sources, situation_type, context_chunks)
    if isinstance(spawn_result, dict):
        result    = spawn_result
        proc      = None
        done_file = None
    else:
        result, proc, done_file = spawn_result

    action = result.get("action", "decline")

    # Handle practice hint feedback
    if action == "hint_feedback" and hint_id is not None:
        fb = result.get("feedback")

        # We must import this here to avoid circular imports and fix the orphaned engine call
        from practice_mode import log_feedback

        if fb in (1, 0):
            log_feedback(hint_id, user_feedback=fb)
            _start_cooldown(5 * 60) # 5 minute cooldown for standard CP feedback
        elif fb == "escalate":
            log_feedback(hint_id, user_feedback=0)  # treat as "not helpful"
            _start_cooldown(0) # NO cooldown - user wants immediate deeper help
            _gen_lock.release()
            return "escalate" # Return this signal back to ipc_client
            
        _gen_lock.release()
        return fb

    if action == "decline":
        _gen_lock.release()
        _start_cooldown(_COOLDOWN_NO)
        print("\033[90m[Notification] User declined. 15-min cooldown.\033[0m", flush=True)
        return

    # ── User clicked YES — NOW run Gemma (GPU inference happens here) ──
    custom_problem = result.get("custom_problem")
    print("\n\033[1;36m[Notification] Generating answer with Gemma...\033[0m", flush=True)
    try:
        if custom_problem:
            # User described their own problem — this needs a fresh Q&A RAG search, NOT a generic insight!
            print("\033[90m[Notification] Fetching specific knowledge for custom problem...\033[0m", flush=True)
            fresh_query = engine.generate_search_query(custom_problem)
            fresh_docs = embedder.search_knowledge_docs(fresh_query, limit=3)
            
            fresh_chunks = []
            fresh_sources = []
            
            # 1. Always inject the current screen context so the AI knows what you're working on right now!
            ctx = screen_context or state.generate_prompt_context(embedder=embedder)
            if ctx:
                fresh_chunks.append(f"[CURRENT SCREEN / RECENT WORK]\n{ctx}")
                fresh_sources.append("Current Screen")
            
            # 2. Add historical knowledge from the vector database
            if fresh_docs:
                structured = engine.build_rag_context(ctx or "", fresh_docs, situation_type)
                fresh_chunks.append(structured)
                fresh_sources.extend([d['topic'] for d in fresh_docs])
            else:
                # Fallback to episodic memory
                memories = embedder.semantic_search(fresh_query, limit=3)
                if memories:
                    fresh_chunks.extend([m["snippet"] for m in memories])
                    fresh_sources.append("past session memory")
                    
            # Use the strict Q&A prompt!
            insight = engine.answer_with_context(custom_problem, fresh_chunks, fresh_sources, screen_context=ctx or "", situation_type=situation_type)
            # Update the sources variable so it gets written to the UI done_file
            sources = fresh_sources
        elif knowledge_docs:
            # Pre-fetched structured docs - build the rich layered context
            ctx = screen_context or state.generate_prompt_context(embedder=embedder)
            structured = engine.build_rag_context(ctx or "", knowledge_docs, situation_type)
            insight = engine.answer_with_context(
                search_query or "", [structured], sources,
                screen_context=ctx or "",
                situation_type=situation_type
            )

        elif context_chunks:
            # Use pre-fetched KNN results (fetched cheaply at idle time, now fed to Gemma)
            ctx = screen_context or ""
            insight = engine.answer_with_context(search_query or "", context_chunks, sources, screen_context=ctx, situation_type=situation_type)
        else:
            # No memory at all — general insight from current screen state
            ctx = screen_context or state.generate_prompt_context(embedder=embedder)
            insight = engine.generate_insight(ctx)
        print("\033[1;32m[Notification] Insight ready.\033[0m", flush=True)
    except Exception as e:
        insight = f"Sorry, I couldn't generate an insight right now.\nError: {e}"
    finally:
        _gen_lock.release()

    _start_cooldown(_COOLDOWN_YES)

    if done_file:
        try:
            with open(done_file, "w", encoding="utf-8") as f:
                json.dump({"insight": insight, "sources": sources}, f)
        except Exception:
            pass

    # Also print it here in the main terminal as a backup
    print("\n\033[1;32m══════════════════════════════════════════════════════\033[0m")
    print("\033[1;32m  💡  Jugnu's Insight\033[0m")
    if sources:
        print(f"\033[90m  📚 Sources: {', '.join(sources)}\033[0m")
    print("\033[1;32m══════════════════════════════════════════════════════\033[0m")
    for line in insight.split("\n"):
        print(f"  {line}")
    print("\033[1;32m══════════════════════════════════════════════════════\033[0m\n")
