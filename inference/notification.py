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

def _spawn_interaction_window(context_summary, sources):
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
        json.dump({"summary": context_summary}, f)

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

# ── Main Orchestrator ─────────────────────────────────────────────────

def trigger_flow(state, engine, embedder,
                 search_query=None, context_chunks=None,
                 sources=None, screen_context=None):
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

    summary = state.get_context_summary()

    spawn_result = _spawn_interaction_window(summary, sources)
    if isinstance(spawn_result, dict):
        result    = spawn_result
        proc      = None
        done_file = None
    else:
        result, proc, done_file = spawn_result

    action = result.get("action", "decline")

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
            fresh_results = embedder.search_knowledge_docs(fresh_query, limit=3)
            
            fresh_chunks = []
            fresh_sources = []
            
            # 1. Always inject the current screen context so the AI knows what you're working on right now!
            ctx = screen_context or state.generate_prompt_context(embedder=embedder)
            if ctx:
                fresh_chunks.append(f"[CURRENT SCREEN / RECENT WORK]\n{ctx}")
                fresh_sources.append("Current Screen")
            
            # 2. Add historical knowledge from the vector database
            if fresh_results:
                for doc in fresh_results:
                    fresh_sources.append(doc['topic'])
                    fresh_chunks.append(f"[PAST KNOWLEDGE: {doc['topic']}]\n{doc['content']}")
            else:
                # Fallback to episodic memory
                memories = embedder.semantic_search(fresh_query, limit=3)
                if memories:
                    fresh_chunks.extend([m["snippet"] for m in memories])
                    fresh_sources.append("past session memory")
                    
            # Use the strict Q&A prompt!
            insight = engine.answer_with_context(custom_problem, fresh_chunks, fresh_sources)
            # Update the sources variable so it gets written to the UI done_file
            sources = fresh_sources
        elif context_chunks:
            # Use pre-fetched KNN results (fetched cheaply at idle time, now fed to Gemma)
            insight = engine.answer_with_context(search_query or "", context_chunks, sources)
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
