from embedder import Embedder
import threading
import time
import json
import os
import subprocess
import sys
import tempfile

# ── Global state ──────────────────────────────────────────────────────

is_generating = False
_last_trigger_time = 0.0
_COOLDOWN_YES = 20 * 60     # 20 min after successful insight
_COOLDOWN_NO  = 15 * 60     # 15 min after dismissal

def in_cooldown():
    return (time.time() - _last_trigger_time) < _COOLDOWN_YES

def _start_cooldown(seconds):
    global _last_trigger_time, _COOLDOWN_YES
    _last_trigger_time = time.time()
    _COOLDOWN_YES = seconds

# ── Core: spawn a new terminal window for the interaction ─────────────

def _spawn_interaction_window(context_summary):
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
        if os.path.exists(f):
            os.remove(f)

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

def trigger_flow(state, engine, embedder=None):
    global is_generating

    if is_generating or in_cooldown():
        return

    # Prepare context summary
    summary = state.get_context_summary()

    # Spawn the new terminal window and wait for the user to respond
    spawn_result = _spawn_interaction_window(summary)
    if isinstance(spawn_result, dict):
        # Error path
        result = spawn_result
        proc = None
        done_file = None
    else:
        result, proc, done_file = spawn_result

    action = result.get("action", "decline")

    if action == "decline":
        _start_cooldown(_COOLDOWN_NO)
        print("\033[90m[Notification] User declined. 15-min cooldown.\033[0m", flush=True)
        return

    # Stage 3: Query AI
    custom_problem = result.get("custom_problem")
    is_generating = True
    print("\n\033[1;36m[Notification] Querying AI...\033[0m", flush=True)
    try:
        context = state.generate_prompt_context(custom_problem=custom_problem, embedder=embedder)
        insight = engine.generate_insight(context)
        print("\033[1;32m[Jugnu AI Buddy] Insight generated successfully.\033[0m\n", flush=True)

    except Exception as e:
        insight = f"Sorry, I couldn't generate an insight right now.\nError: {e}"

    finally:
        is_generating = False

    _start_cooldown(_COOLDOWN_YES)

    # Write the insight back to the .done file so the terminal window displays it
    if done_file:
        try:
            with open(done_file, "w", encoding="utf-8") as f:
                json.dump({"insight": insight}, f)
        except Exception:
            pass

    # Also print it here in the main terminal as a backup
    print("\n\033[1;32m══════════════════════════════════════════════════════\033[0m")
    print("\033[1;32m  💡  Jugnu's Insight\033[0m")
    print("\033[1;32m══════════════════════════════════════════════════════\033[0m")
    for line in insight.split("\n"):
        print(f"  {line}")
    print("\033[1;32m══════════════════════════════════════════════════════\033[0m\n")
