import difflib
import sqlite3
import os
import subprocess
import json
import threading
import sys
from practice_mode import (
    get_or_create_session,
    mark_session_solved,
    update_session,
    flush_session_to_db,
    log_feedback,
    log_hint,
    get_last_hints
)

class CPEventHandler:
    def __init__(self):
        self.active_session = None
        self.abort_hint_generation = False
        self.cached_hint = None
        self.cached_code_snapshot = None
        self.last_uia_row_id = None
        self.mascot = None

    def _fetch_problem_context(self):
        if not self.active_session:
            return "Problem Context Placeholder"
        
        slug = self.active_session["problem_slug"]
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'jugnu.db')

        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()

            if getattr(self, 'last_uia_row_id', None) is not None:
                cur.execute("SELECT content FROM knowledge_docs WHERE id = ?", (self.last_uia_row_id,))
            else:
                # Fallback just in case
                cur.execute("SELECT content FROM knowledge_docs WHERE source_url LIKE ? OR window_title LIKE ? ORDER BY last_updated DESC LIMIT 1", (f'%{slug}%', f'%{slug}%'))
                
            row = cur.fetchone()
            conn.close()

            return row[0] if row else "Problem Context Placeholder"
        except Exception as e:
            print(f"\033[1;31m[DB Error]\033[0m Could not fetch context: {e}")
            return "Problem Context Placeholder"
    
    def handle_reading_idle(self, engine, code=""):
        slug = self.active_session.get("problem_slug", "") if self.active_session else ""
        print(f"\033[1;33m[UI]\033[0m CP_READING_IDLE — setting mascot nudge state, spawning bubble for: {slug}")

        # Set mascot to nudge state immediately
        if hasattr(self, 'mascot') and self.mascot:
            self.mascot.set_state('nudge')
        
        launcher_path = os.path.join(os.path.dirname(__file__), '..', 'ui', 'launcher.py')

        # write the state file so the bubble knows what slug to display
        nudge_state = {
            "slug":        slug,
            "nudge_type":  "reading",
            "nudge_title": f"Still on {slug}?",
            "nudge_msg":   f"You've been reading <strong>{slug}</strong> for a while — want a starting <strong>hint</strong>?"
        }
        state_file = os.path.join(os.path.dirname(__file__), '..', 'ui', 'nudge_state.json')
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(nudge_state, f)
        
        # Spawn bubble — can overlap with the 5s mascot animation, no issue
        process = subprocess.Popen(
            [sys.executable, launcher_path, 'nudge_bubble', state_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Drain stderr so any crash in the bubble process is visible in the terminal
        def _drain_stderr(proc, label):
            for line in iter(proc.stderr.readline, ''):
                line = line.strip()
                if line:
                    print(f"\033[1;31m[{label} stderr]\033[0m {line}", flush=True)
        threading.Thread(target=_drain_stderr, args=(process, 'NudgeBubble'), daemon=True).start()

        def ui_listener():
            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue
                try:
                    action_data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if action_data.get("event") != "nudge_action":
                    continue

                if action_data.get("action") == "hint":
                    print("\033[1;32m[UI]\033[0m User clicked Help! Asking Gemma for a starting hint...")
                    if hasattr(self, 'mascot') and self.mascot:
                        self.mascot.set_state('thinking')
                    
                    problem_context = self._fetch_problem_context()
                    hint_history = get_last_hints(self.active_session["id"], n = 3) if self.active_session else []

                    result = engine.check_code_correctness(code, problem_context, hint_history = hint_history)

                    # Log the hint to DB
                    hint_id = None
                    if self.active_session:
                        hint_id = log_hint(
                            self.active_session["id"],
                            hint_type=result.get("hint_type") or "CONCEPTUAL",
                            hint_text=result.get("content", ""),
                            user_state="READING",
                            code_snapshot=code,
                            approach=result.get("approach", "unknown"),
                        )
                    
                    ui_state = {
                        "mode": "practice_hint",
                        "hint_text": result.get("content", ""),
                        "approach": result.get("approach", "Unknown"),
                        "hint_level": 1
                    }

                    ui_state_file = os.path.join(os.path.dirname(__file__), '..', 'ui', 'ui_state.json')
                    with open(ui_state_file, 'w', encoding='utf-8') as f:
                        json.dump(ui_state, f)
                    
                    sidebar_proc = subprocess.Popen(
                        [sys.executable, launcher_path, 'sidebar', ui_state_file],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                    )
                    threading.Thread(
                        target=lambda p: [print(f'\033[1;31m[Sidebar stderr]\033[0m {l.strip()}', flush=True)
                                          for l in iter(p.stderr.readline, '') if l.strip()],
                        args=(sidebar_proc,), daemon=True
                    ).start()
                    if hasattr(self, 'mascot') and self.mascot:
                        self.mascot.set_state('hint_ready')
                    
                    # Start sidebar feedback reader if we have a hint_id
                    if hint_id and self.active_session:
                        threading.Thread(
                            target = self._sidebar_feedback_reader,
                            args=(sidebar_proc, hint_id, "", problem_context, hint_history, engine),
                            daemon=True
                        ).start()
                elif action_data.get("action") == "dismiss":
                    print("\033[1;33m[UI]\033[0m User dismissed nudge bubble.")

                    if hasattr(self, 'mascot') and self.mascot:
                        self.mascot.set_state('sleeping')

        threading.Thread(target=ui_listener, daemon=True).start()

    
    def handle_session_start(self, slug, platform):
        self.active_session = get_or_create_session(slug, platform)

        # CONTINUATION: Load the last hint's code snapshot from the DB if it exists!
        if self.active_session and self.active_session.get("code_snapshot"):
            self.cached_code_snapshot = self.active_session["code_snapshot"]
            print(f"\033[1;32m[CPHandler]\033[0m Restored previous code snapshot for delta checking.")

        print(f"\033[1;36m[CPHandler]\033[0m Session Started/Resumed for {platform}: {slug}")

    def handle_session_end(self, code=""):
        if not self.active_session:
            return
            
        print(f"\033[1;31m[CPHandler]\033[0m Session Ended. Saving state to DB...")
        
        # Persist final code snapshot if we have one
        final_code = code if code else self.cached_code_snapshot
        if final_code:
            update_session(self.active_session["problem_slug"], self.active_session["platform"], code_snapshot=final_code)
        
        flush_session_to_db(self.active_session["problem_slug"])
        
        self.active_session = None
        self.cached_hint = None
        self.cached_code_snapshot = None
        self.abort_hint_generation = False

    def handle_stuck(self, code, engine):
        if self.active_session and self.active_session.get("is_solved") == 1:
            print("\033[1;32m[CPHandler]\033[0m Session already solved. Ignoring STUCK event.")
            return
        
        self.abort_hint_generation = False

        # --- Caching Delta Logic ---
        if self.cached_hint and self.cached_code_snapshot:
            # Compare current code with the code we generated the hint for
            ratio = difflib.SequenceMatcher(None, self.cached_code_snapshot, code).ratio()

            if ratio > 0.90:
                print("\033[1;32m[CPHandler]\033[0m High code similarity (>90%)! Serving CACHED hint instantly.")
                self._spawn_sidebar_with_result(
                    {"content": self.cached_hint, "is_solved": 0, "approach": "cached", "hint_type": "CACHED"},
                    code, engine, hint_id=None, is_cached=True
                )
                return
            else:
                print("\033[1;33m[CPHandler]\033[0m Low code similarity. Cache is stale, asking Gemma...")

        print("\033[1;36m[CPHandler]\033[0m Fetching problem context from DB and Querying Gemma...")
        if hasattr(self, 'mascot') and self.mascot:
            self.mascot.set_state('thinking')

        problem_context = self._fetch_problem_context()
        hint_history = get_last_hints(self.active_session["id"],n=3) if self.active_session else []

        result = engine.check_code_correctness(code, problem_context, hint_history = hint_history)

        # Check Race Condition Flag - did they type while Gemma was thinking?
        if self.abort_hint_generation:
            print("\033[1;32m[CPHandler]\033[0m ABORT FLAG CAUGHT! User resumed typing. Caching hint quietly.")
            self.cached_hint = result["content"]
            self.cached_code_snapshot = code
            return
        
        self._spawn_sidebar_with_result(result, code, engine,
                                        hint_id=None, problem_context=problem_context,
                                        hint_history=hint_history)
    
    def _spawn_sidebar_with_result(self, result, code, engine, hint_id = None, problem_context = "", hint_history = None, is_cached = False):
        """Writes ui_state.json, spawns sidebar, logs hint, starts feedback reader."""
        hint_history = hint_history or []
        launcher_path = os.path.join(os.path.dirname(__file__), '..', 'ui', 'launcher.py')
        state_file = os.path.join(os.path.dirname(__file__), '..', 'ui', 'ui_state.json')

        if result.get("is_solved", 0) == 1:
            if self.active_session:
                mark_session_solved(self.active_session["id"])
            print("\033[1;32m[UI]\033[0m Problem solved! Spawning sidebar with review.")
            ui_state = {
                "mode": "practice_solved",
                "hint_text": "Awesome! You solved it!\n\n" + result.get("content", ""),
                "slug": self.active_session["problem_slug"] if self.active_session else "Unknown",
                "platform": self.active_session["platform"] if self.active_session else "Unknown",
                "history": hint_history
            }
            if hasattr(self, 'mascot') and self.mascot:
                self.mascot.set_state('hint_ready')
        else:
            self.cached_hint = result["content"]
            self.cached_code_snapshot = code
            print("\033[1;33m[UI]\033[0m Spawning sidebar with Gemma hint.")
            ui_state = {
                "mode": "practice_hint",
                "hint_text": result.get("content", ""),
                "approach": result.get("approach", "Unknown"),
                "hint_type": result.get("hint_type", "CONCEPTUAL"),
                "slug": self.active_session["problem_slug"] if self.active_session else "Unknown",
                "platform": self.active_session["platform"] if self.active_session else "Unknown",
                "history": hint_history
            }
            # Persist snapshot to DB for continuation across restarts
            if self.active_session and not is_cached:
                update_session(self.active_session["problem_slug"], self.active_session["platform"], code_snapshot=code)
                flush_session_to_db(self.active_session["problem_slug"])

            # Log hint to DB
            if self.active_session and not is_cached:
                hint_id = log_hint(
                    self.active_session["id"],
                    hint_type=result.get("hint_type") or "CONCEPTUAL",
                    hint_text=result.get("content", ""),
                    user_state="STUCK",
                    code_snapshot=code,
                    approach=result.get("approach", "unknown"),
                )
            
            if hasattr(self, 'mascot') and self.mascot:
                self.mascot.set_state('hint_ready')
        
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(ui_state, f)
        
        sidebar_proc = subprocess.Popen(
            [sys.executable, launcher_path, 'sidebar', state_file],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        threading.Thread(
            target=lambda p: [print(f'\033[1;31m[Sidebar stderr]\033[0m {l.strip()}', flush=True)
                              for l in iter(p.stderr.readline, '') if l.strip()],
            args=(sidebar_proc,), daemon=True
        ).start()

        if hint_id and self.active_session:
            threading.Thread(
                target=self._sidebar_feedback_reader,
                args=(sidebar_proc, hint_id, code, problem_context, hint_history, engine),
                daemon=True
            ).start()
    
    def _sidebar_feedback_reader(self, proc, hint_id, code, problem_context, hint_history, engine):
        """Reads feedback events from the sidebar process stdout."""
        for line in iter(proc.stdout.readline, ''):
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            if data.get("event") != "feedback":
                continue

            f_type = data.get("type")
            if f_type == "helpful":
                log_feedback(hint_id, user_feedback=1)
                print("\033[1;32m[CPHandler]\033[0m User found hint helpful. Logged.")
            elif f_type == "not_helpful":
                log_feedback(hint_id, user_feedback=0)
                print("\033[1;33m[CPHandler]\033[0m User found hint not helpful. Logged.")
            elif f_type == "escalate":
                log_feedback(hint_id, user_feedback=0)
                print("\033[1;33m[CPHandler]\033[0m User escalated — generating deeper hint...")
                if hasattr(self, 'mascot') and self.mascot:
                    self.mascot.set_state('thinking')
            
                # Pass the failed hint into history so Gemma can't repeat it
                extended_history = hint_history + [self.cached_hint] if self.cached_hint else hint_history
                new_result = engine.check_code_correctness(
                    code, problem_context, hint_history=extended_history
                )
                # Spawn a fresh sidebar with the deeper hint — feedback reader starts fresh too
                self._spawn_sidebar_with_result(
                    new_result, code, engine,
                    problem_context=problem_context,
                    hint_history=extended_history
                )
            break  # One feedback event per sidebar session is enough


    def handle_typing_resumed(self):
        print("\033[1;33m[CPHandler]\033[0m Caught CP_USER_RESUMED! Setting abort flag.")
        self.abort_hint_generation = True
