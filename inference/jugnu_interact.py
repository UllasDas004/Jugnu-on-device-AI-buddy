"""
jugnu_interact.py  —  Runs inside a NEW terminal window.

Usage:  python jugnu_interact.py <path_to_state.json> <path_to_result.json>

Reads the context from state.json, asks the user what they want,
writes the decision to result.json, then exits so the main
process can read it.
"""
import sys
import json
import os

# ── Helpers ────────────────────────────────────────────────────────────

def _print_box(title, body_lines, color="35"):
    width = 60
    bar = f"\033[1;{color}m" + "═" * width + "\033[0m"
    print(f"\n{bar}", flush=True)
    print(f"\033[1;{color}m  {title}\033[0m", flush=True)
    print(bar, flush=True)
    for line in body_lines:
        print(f"  {line}", flush=True)
    print(bar, flush=True)

def _ask(prompt, valid):
    """Keep asking until we get a valid answer."""
    while True:
        try:
            ans = input(prompt).strip().lower()
            if not ans:
                ans = valid[0]           # default = first option
            if ans in valid:
                return ans
            print(f"  Please type one of: {', '.join(valid).upper()}")
        except (EOFError, KeyboardInterrupt):
            return valid[-1]             # default = last (cancel/no)

# ── Main interaction ────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 3:
        print("Usage: python jugnu_interact.py <state.json> <result.json>")
        sys.exit(1)

    state_path  = sys.argv[1]
    result_path = sys.argv[2]

    # Read context written by notification.py
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    context_summary = state.get("summary", "No context available.")

    result = {"action": "cancel", "custom_problem": ""}

    mode      = state.get("mode", "general")
    hint_text = state.get("hint_text", "")
    if mode == "practice_hint" and hint_text:
        sources_str = ", ".join(state.get("sources", []))
        _print_box(
            f"🧠  Jugnu — Practice Hint",
            [
                f"\033[90m{sources_str}\033[0m" if sources_str else "",
                "",
                hint_text,
                "",
                "\033[1;32m[1]\033[0m  ✓  Helpful — got it, let me try",
                "\033[1;31m[2]\033[0m  ✗  Not helpful — different angle please",
                "\033[1;33m[3]\033[0m  ↓  Go deeper — still stuck",
                "\033[90m[N]\033[0m  Dismiss",
            ],
            color="36"
        )
        ans = _ask("\n  Your choice (1/2/3/N) [default: N]: ", ["1", "2", "3", "n"])
        fb_map = {"1": 1, "2": 0, "3": "escalate", "n": "dismiss"}
        feedback = fb_map.get(ans, "dismiss")
        result = {"action": "hint_feedback", "feedback": feedback}
        _write_and_exit(result, result_path)
        return

    # ── Stage 1: Nudge ─────────────────────────────────────────────────
    _print_box("🧠  Jugnu — Your AI Coding Buddy", [
        "You've been working for a while.",
        "I noticed some interesting context in your session.",
        "",
        "\033[1;32m[Y]\033[0m  Yes, show me an insight!",
        "\033[1;31m[N]\033[0m  No thanks, I'm fine.",
    ], color="36")

    ans = _ask("\n  Your choice (Y/N) [default: Y]: ", ["y", "yes", "n", "no"])
    if ans in ("n", "no"):
        result["action"] = "decline"
        _write_and_exit(result, result_path)
        return

    # ── Stage 2: Context confirm ────────────────────────────────────────
    _print_box("📋  Here's What I Know About Your Session", [
        context_summary,
        "",
        "\033[1;33m[Y]\033[0m  Use this context — generate insight!",
        "\033[1;33m[C]\033[0m  Let me describe my problem manually.",
        "\033[1;31m[N]\033[0m  Cancel.",
    ], color="33")

    ans = _ask("\n  Your choice (Y/C/N) [default: Y]: ", ["y", "yes", "c", "custom", "n", "no"])

    if ans in ("n", "no"):
        result["action"] = "decline"
        _write_and_exit(result, result_path)
        return

    if ans in ("c", "custom"):
        # ── Stage 2b: Custom problem ────────────────────────────────────
        _print_box("✏️   Describe Your Problem", [
            "What exactly are you stuck on?",
            "Type your question and press Enter.",
        ], color="33")
        try:
            problem = input("\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            problem = ""
        if not problem:
            result["action"] = "decline"
            _write_and_exit(result, result_path)
            return
        result["action"] = "custom"
        result["custom_problem"] = problem
    else:
        result["action"] = "yes"

    # ── Waiting message ─────────────────────────────────────────────────
    print("\n\033[1;36m  ⏳  Jugnu is querying the AI model...\033[0m", flush=True)
    print("  (You can switch back to your work — the insight will appear here)\n", flush=True)

    _write_and_exit(result, result_path)


def _write_and_exit(result, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f)
    # Keep window open so user can read the final insight when we write it back
    if result.get("action") not in ("decline", "cancel"):
        # Wait here — notification.py will overwrite result.json with the insight
        print("\033[90m  [Waiting for AI response...]\033[0m", flush=True)
        import time
        marker_path = path + ".done"

        # P2-FIX: Add a timeout so this window doesn't hang forever if
        # notification.py crashes mid-generation and never writes the done file.
        deadline = time.time() + 180  # 3 minute hard limit

        while not os.path.exists(marker_path):
            if time.time() > deadline:
                print("\n  \033[1;31m[Error] Timed out waiting for AI response. The engine may have crashed.\033[0m")
                print("  You can close this window.")
                input()
                return
            time.sleep(0.3)
        # Read and display the insight
        with open(marker_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        insight = data.get("insight", "No insight generated.")
        width = 60
        bar = "\033[1;32m" + "═" * width + "\033[0m"
        print(f"\n{bar}", flush=True)
        print("\033[1;32m  💡  Jugnu's Insight\033[0m", flush=True)
        print(bar, flush=True)
        for line in insight.split("\n"):
            print(f"  {line}", flush=True)
        print(bar, flush=True)
        os.remove(marker_path)
        input("\n  Press Enter to close this window...")

if __name__ == "__main__":
    main()
