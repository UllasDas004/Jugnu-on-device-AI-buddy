import threading
import win32file
import win32pipe
import pywintypes
import os
import subprocess
import sys
import time
import json
from state_manager import StateManager
from ai_engine import AIEngine
from embedder import Embedder
from flush_worker import (
    FlushWorker
)
from core.cp_event_handler import CPEventHandler
from sentence_transformers import SentenceTransformer
import ollama

# ANSI colors for terminal logging
_CYAN   = "\033[1;36m"
_GREEN  = "\033[1;32m"
_RED    = "\033[1;31m"
_YELLOW = "\033[1;33m"
_RESET  = "\033[0m"

class MascotController:
    """
    Holds the jugnuBug subprocess and lets any part of the system
    change its animation state with a single call.
    """
    def __init__(self):
        self._proc = None
        self._dash_proc = None
        self._watching_timer = None   # auto-revert timer for 'watching' state
        self.current_state = 'sleeping'
    
    def spawn(self, launcher_path: str):
        """Spawn the always-on jugnuBug mascot process at startup."""
        self._proc = subprocess.Popen(
            [sys.executable, launcher_path, 'jugnu_bug'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            creationflags=0x00008000 # ABOVE_NORMAL_PRIORITY_CLASS
        )
        print("\033[1;35m[Mascot]\033[0m jugnuBug spawned.")

    def start_output_reader(self, engine, embedder, launcher_path):
        """Reads mascot stdout for toggle_dashboard / set_bug_state events."""
        def _reader():
            for line in iter(self._proc.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("type") == "toggle_dashboard":
                        if self._dash_proc and self._dash_proc.poll() is None:
                            # It's already running, toggle it off by killing it
                            try:
                                self._dash_proc.terminate()
                            except Exception:
                                pass
                            self._dash_proc = None
                        else:
                            # Not running, spawn it
                            stats = _query_dashboard_stats(embedder)
                            state_file = os.path.join(os.path.dirname(launcher_path), 'dashboard_state.json')
                            with open(state_file, 'w', encoding='utf-8') as f:
                                json.dump({"dashboard": stats}, f)
                            
                            self._dash_proc = subprocess.Popen(
                                [sys.executable, launcher_path, 'dashboard', state_file],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                            )
                            self._watch_subprocess(self._dash_proc)
                    elif data.get("type") == "set_bug_state":
                        # Mascot debug state override (shouldn't happen from mascot
                        # stdout, but handle defensively)
                        self.set_state(data.get("state", "sleeping"))
                except Exception:
                    pass
        threading.Thread(target=_reader, daemon=True).start()

    def _watch_subprocess(self, proc):
        """Reads stdout of any child process (e.g. dashboard) for set_bug_state events."""
        def _reader():
            for line in iter(proc.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("type") == "set_bug_state":
                        self.set_state(data.get("state", "sleeping"))
                except Exception:
                    pass
        threading.Thread(target=_reader, daemon=True).start()

    
    def set_state(self, state_name: str, background_event: bool = False):
        """
        Tell the mascot HTML to switch animation state via stdin.
        The HTML is a pure renderer with no internal timers — all state
        lifetime logic lives here so individual features can set their own
        duration without touching the UI layer.
        """
        if background_event and self.current_state in ('thinking', 'hint_ready'):
            return

        self.current_state = state_name

        # Cancel any pending auto-revert before applying the new state
        if self._watching_timer is not None:
            self._watching_timer.cancel()
            self._watching_timer = None

        if not self._proc or self._proc.poll() is not None:
            return  # Process died, ignore silently
        try:
            self._proc.stdin.write(json.dumps({"cmd": "set_state", "state": state_name}) + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            print(f"\033[1;31m[Mascot]\033[0m Failed to set state: {e}")
            return

        # Only the 'watching' state triggered by app-switch events has an
        # auto-revert. All other states (hint_ready, thinking, etc.) are
        # expected to be explicitly cleared by their owning feature.
        if state_name == 'watching':
            def _revert():
                print("\033[90m[Mascot] Watching timer expired — reverting to sleeping.\033[0m", flush=True)
                self.set_state('sleeping', background_event=True)
            self._watching_timer = threading.Timer(15.0, _revert)
            self._watching_timer.daemon = True
            self._watching_timer.start()
    
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


# Fix CUDA warmup crash on RTX 4050 / Turing+ with Ollama's Flash Attention PDL kernel
os.environ.setdefault("OLLAMA_FLASH_ATTENTION", "0")

def _is_online() -> bool:
    """Quick connectivity check - try to reach Ollama's update endpoint."""
    import socket
    try:
        # P2-FIX: Context manager closes the socket FD immediately after check.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            _s.settimeout(3)
            _s.connect(("8.8.8.8", 53))
        return True
    except Exception:
        return False
        
def ensure_models_downloaded():
    print("[INIT] Checking AI Models... Please wait.")

    online = _is_online()
    if not online:
        print("[INIT] Offline mode detected. Will only use locally cached models.")
    
    # ── 1. e5-small-v2 (SentenceTransformer / HuggingFace) ──────────────────
    try:
        print("[INIT] Loading intfloat/e5-small-v2...")
        # SentenceTransformer loads from local HF cache automatically.
        # If cache is missing AND we're offline, this will raise and we exit —
        # because we genuinely cannot run without the embedder.
        # We pass local_files_only=True if offline to prevent HuggingFace from 
        # trying to check for updates (which throws getaddrinfo failed).
        model = SentenceTransformer('intfloat/e5-small-v2', local_files_only=not online)
        print("[INIT] e5-small is ready!")
    except Exception as e:
        print(f"[FATAL] e5-small not available locally and cannot download: {e}")
        sys.exit(1)

    # ── 2. Gemma (Ollama) ────────────────────────────────────────────────────
    model_name = "gemma4:e2b"
    try:
        ollama.show(model_name)
        # Model exists locally — we're done regardless of internet status
        print(f"[INIT] {model_name} is ready!")
        return
    
    except ollama.ResponseError:
        # Model NOT found locally
        if not online:
            # Offline AND model missing - cannot proceed
            print(f"[FATAL] {model_name} is not cached locally and you are offline.")
            print("Please connect to the internet once to download the model, then run again.")
            sys.exit(1)
        else:
            # Online AND model missing - pull it
            print(f"[INIT] {model_name} not found locally. Downloading (this may take a few minutes)...")
            try:
                ollama.pull(model_name)
                print(f"[INIT] Successfully downloaded {model_name}!")
            except Exception as e:
                print(f"[FATAL] Failed to pull Ollama model: {e}")
                print("Make sure the Ollama background service is running: 'ollama serve'")
                sys.exit(1)
    except Exception as e:
        # ollama service itself is not running
        print(f"[FATAL] Cannot reach Ollama service: {e}")
        print("Start it with: ollama serve")
        sys.exit(1)


PIPE_NAME = r"\\.\pipe\jugnu_ipc"

def connect_to_pipe():
    print(f"[Python] Attempting to connect to {PIPE_NAME}...")

    while True:
        try:
            # Try to open the pipe created by our C++ IPCServer
            handle = win32file.CreateFile(
                PIPE_NAME,
                win32file.GENERIC_READ,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None
            )
            print("[Python] Successfully connected to C++ Engine!")
            return handle
        except pywintypes.error as e:
            if e.winerror == 2: # ERROR_FILE_NOT_FOUND
                print("Pipe not found. Is Jugnu.exe running?")
                time.sleep(2)
            elif e.winerror == 231: # ERROR_PIPE_BUSY
                print("Pipe is busy. Retrying...")
                time.sleep(1)

def _synthesize_and_save_file(engine, embedder, app_name, filepath, code_text):
    """
    Background thread: treats a saved code file as a verbatim code snippet,
    wraps it in a pseudo-extraction, and saves it via the new column-split pipeline.
    Only runs for files under 20000 chars to avoid VRAM overruns.
    """
    MAX_FILE_CHARS = 20000

    if len(code_text) > MAX_FILE_CHARS:
        print(f"{_YELLOW}[OKF] File too large ({len(code_text)} chars). Episodic-only.{_RESET}")
        return

    if not code_text.strip():
        return

    # Treat the whole file as a verbatim code snippet — no Gemma extraction needed for code files
    filename = os.path.basename(filepath) if filepath else "clipboard"
    pseudo_ext = {
        "content":  code_text,
        "tags":     ["code", os.path.splitext(filename)[1].lstrip(".") or "unknown"],
        "notes":    "",
        "topic":    filename,
        "verbatim": True,
    }
    doc = engine.combine_sections([pseudo_ext], file_path=filepath)
    if doc:
        saved = embedder.save_knowledge_doc(app_name, doc, engine)
        if saved:
            print(f"{_GREEN}[OKF] Knowledge doc saved from FILE_SAVED: {filename}{_RESET}")
        else:
            print(f"{_YELLOW}[OKF] Save failed for {filename} — episodic only.{_RESET}")

# Global stop flag — set by main thread on Ctrl+C, read by reader daemon
_stop_event = threading.Event()


def _idle_handler_background(state, engine, embedder, screen_context, ipc_code="", target_app=""):
    """
    Runs in a background thread. Keeps the IPC listener free.
    Does: LLM query generation → KNN search → situation detection → notification.
    """
    print(f"{_YELLOW}[IPC-BG] Generating search query for Generic Idle...{_RESET}")
    search_query = engine.generate_search_query(screen_context)
    print(f"{_YELLOW}[IPC-BG] KNN Query: '{search_query}'{_RESET}")

    knowledge_docs = embedder.search_knowledge_docs(search_query, engine, limit = 3)
    context_chunks = []
    sources = []
    situation_type = "NO_MEMORY"

    if knowledge_docs:
        top           = knowledge_docs[0]
        capture_count = top.get('capture_count', 1)
        source_type   = top.get('source_type', '')
        code_snippet  = top.get('code_snippet', '')
        
        if capture_count >= 4:
            situation_type = "REPEATED_STRUGGLE"
        elif source_type == 'ide' or (source_type == 'browser' and code_snippet):
            situation_type = "STUCK_ON_OWN_CODE"        
        elif source_type == 'browser':
            situation_type = "READING_NEW_MATERIAL"     
        else:
            situation_type = "GENERAL"
        sources = [doc['topic'] for doc in knowledge_docs]
        print(f"{_GREEN}[IPC-BG] Situation: {situation_type} | Docs: {len(knowledge_docs)}{_RESET}")
        
    else:
        memories = embedder.semantic_search(search_query, limit=3)
        if memories:
            context_chunks = [m["snippet"] for m in memories]
            sources        = ["past session memory"]
            situation_type = "GENERAL"
            print(f"{_GREEN}[IPC-BG] Falling back to {len(memories)} episodic memories.{_RESET}")
        else:
            print(f"{_YELLOW}[IPC-BG] No memory found. Notification will use general insight.{_RESET}")
    if not context_chunks and knowledge_docs:
        context_chunks = [d['summary'] for d in knowledge_docs if d.get('summary')]
    
    notification_msg = engine.generate_helpful_nudge(context_chunks, situation_type, ipc_code)
    
    if notification_msg:
        print(f"\n{_YELLOW}================================{_RESET}")
        print(f"{_YELLOW}💡 {notification_msg}{_RESET}")
        print(f"{_YELLOW}================================{_RESET}\n")
        
        # Fire the generic Nudge Bubble with a proper state file
        import subprocess
        launcher_path = os.path.join(os.path.dirname(__file__), 'ui', 'launcher.py')
        nudge_state_file = os.path.join(os.path.dirname(__file__), 'ui', 'nudge_state.json')
        nudge_state = {
            "nudge_type":  "idle",
            "nudge_title": "Ready to dive back in?",
            "nudge_msg":   notification_msg,
        }
        try:
            with open(nudge_state_file, 'w', encoding='utf-8') as _f:
                json.dump(nudge_state, _f)
        except Exception:
            pass
        subprocess.Popen(
            [sys.executable, launcher_path, 'nudge_bubble', nudge_state_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

def _pipe_reader_daemon(handle, state, engine, embedder, mascot):
    """
    Runs in a daemon thread. Uses PeekNamedPipe to avoid blocking
    so the main thread remains free to catch KeyboardInterrupt.
    """
    buffer = ""
    while not _stop_event.is_set():
        try:
            # PeekNamedPipe is non-blocking: returns instantly with bytes_available.
            # This is the key fix — we NEVER block inside a kernel call without a way out.
            _, bytes_available, _ = win32pipe.PeekNamedPipe(handle, 0)

            if bytes_available == 0:
                # No data yet — sleep briefly and yield back to Python scheduler
                # This tiny sleep is what lets the main thread catch KeyboardInterrupt
                time.sleep(0.05)
                continue

            # Data IS available — safe to call ReadFile, it will return immediately
            result, data = win32file.ReadFile(handle, 4096)
            if result == 0:
                # pywin32 returns bytes from a binary pipe, but stubs declare str|bytes.
                # This guard handles both safely.
                chunk = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
                buffer += chunk

                # Check for the delimeter we defined in C++
                if "END_OF_MSG\n" in buffer:
                    messages = buffer.split("END_OF_MSG\n")

                    for msg in messages[:-1]:
                        msg = msg.strip()
                        if msg:
                            try:
                                # Parse the C++ JSON payload
                                payload = json.loads(msg)
                                print(f"\n\033[1;36m======================================\033[0m", flush=True)
                                print(f"🔥 \033[1;32mRECEIVED EVENT: {payload.get('type')}\033[0m", flush=True)
                                print(f"\033[1;36m======================================\033[0m", flush=True)
                                print(f"\033[33m{json.dumps(payload, indent=2)}\033[0m", flush=True)

                                event_type = payload.get('type')

                                if event_type == 'SWITCH':
                                    app = payload.get('current_app', '')
                                    state.update_switch(app, payload.get('predicted_next', []))

                                    mascot.set_state('watching', background_event=True)  # 15s watching anim on any switch
                                    
                                    # Since C++ now strictly filters for whitelisted Deep Work apps,
                                    # every SWITCH event is guaranteed to be a coding app.
                                    # FIX IPC-1: Don't track terminals as coding apps
                                    if app.lower() not in {"windowsterminal.exe", "pwsh.exe", "cmd.exe"}:
                                        state.set_last_coding_app(app)
                                elif event_type == 'CLIPBOARD':
                                    text = payload.get('text', '')
                                    state.update_clipboard(text)
                                    # Save to semantic memory if it's substantial text
                                    if text and len(text.strip()) > 20:
                                        threading.Thread(
                                            target=embedder.save_memory,
                                            args=(state.current_app or 'unknown', state.current_app or 'clipboard', text),
                                            kwargs={'file_path': None},
                                            daemon=True
                                        ).start()
                                    # If it looks like a code snippet, synthesize into OKF
                                    if text and len(text.strip()) > 100:
                                        threading.Thread(
                                            target=_synthesize_and_save_file,
                                            args=(engine, embedder, state.current_app or 'clipboard', 'clipboard', text),
                                            daemon=True
                                        ).start()

                                elif event_type == "FILE_SAVED":
                                    filepath = payload.get('file')
                                    state.update_file(filepath)
                                    # Read the file and save to semantic memory
                                    if filepath and os.path.exists(filepath):
                                        try:
                                            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                                                code_text = f.read()
                                            threading.Thread(
                                                target=embedder.save_memory,
                                                args=(state.current_app or 'unknown', state.current_app or 'unknown', code_text),
                                                kwargs={'file_path': filepath},
                                                daemon=True
                                            ).start()

                                            threading.Thread(
                                                target=_synthesize_and_save_file,
                                                args=(engine, embedder, state.current_app or 'unknown', filepath, code_text),
                                                daemon=True
                                            ).start()
                                        except Exception as e:
                                            print(f"\033[1;31m[Embedder] Could not read file: {e}\033[0m")
                                elif event_type == "USER_IDLE":
                                    if state.was_recently_coding():
                                        # Take an instant snapshot — no LLM calls on the IPC thread
                                        # FIX IPC-2: Trust the payload provided by C++!
                                        idle_app = payload.get('current_app', state.last_coding_app)
                                        ipc_code = payload.get('code', '')

                                        screen_context_snapshot = ""
                                        # Only generate heavy context if we don't have fresh code over IPC
                                        if not ipc_code:
                                            screen_context_snapshot = state.generate_prompt_context(embedder=None, target_app = idle_app)
                                        
                                        threading.Thread(
                                            target=_idle_handler_background,
                                            args=(state, engine, embedder, screen_context_snapshot, ipc_code, idle_app),
                                            daemon=True
                                        ).start()
                                    else:
                                        print("\033[90m[System] No recent coding context. Skipping.\033[0m", flush=True)
                                
                                elif event_type == "UIA_EXTRACTION_SAVED":
                                    mascot.set_state('watching', background_event=True)  # 15s watching anim on any switch
                                    row_id = payload.get('row_id')
                                    is_new = payload.get('is_new', True)
                                    if row_id is not None:
                                        print(f"{_CYAN}[IPC] Received UIA_EXTRACTION_SAVED for row_id: {row_id} (is_new: {is_new}){_RESET}")
                                        cp_handler.last_uia_row_id = row_id
                                        # Spawn thread to avoid blocking the IPC pipe
                                        threading.Thread(
                                            target = flush_worker.process_uia_by_id,
                                            args=(row_id, engine, embedder, is_new),
                                            daemon=True
                                        ).start()
                                elif event_type == "CP_SESSION_START":
                                    slug = payload.get("slug", "")
                                    platform = payload.get("platform", "leetcode")
                                    cp_handler.handle_session_start(slug, platform)
                                    mascot.set_state('watching', background_event=True)
                                elif event_type == "CP_SESSION_END":
                                    code = payload.get("code", "")
                                    cp_handler.handle_session_end(code)
                                elif event_type == "CP_READING_IDLE":
                                    code = payload.get("code", "")
                                    cp_handler.handle_reading_idle(engine, code)
                                elif event_type == "CP_STUCK":
                                    code = payload.get("code", "")
                                    # Run in a background thread so the pipe reader isn't blocked by Gemma!
                                    threading.Thread(target=cp_handler.handle_stuck, args=(code, engine), daemon=True).start()
                                elif event_type == "CP_USER_RESUMED":
                                    cp_handler.handle_typing_resumed()

                            except json.JSONDecodeError:
                                print(f"\033[1;31m[Error] Failed to decode JSON:\033[0m {msg}", flush=True)
                    
                    #Keep any remainder for the next loop
                    buffer = messages[-1]

        except pywintypes.error as e:
            if e.winerror == 109 or e.winerror == 233:  # ERROR_BROKEN_PIPE or ERROR_PIPE_NOT_CONNECTED
                print("\n[Python] C++ engine disconnected. Reconnecting...", flush=True)
                win32file.CloseHandle(handle)
                if _stop_event.is_set():
                    return
                # Prevent tight loops if C++ is completely dead
                time.sleep(1)
                handle = connect_to_pipe()
            else:
                print(f"[Python] Pipe read error: {e}", flush=True)
                return
        except Exception as e:
            print(f"\n[Python] Unexpected error in reader: {e}", flush=True)
            return


def _query_dashboard_stats(embedder):
    """
    Queries the DB to build the full dashboard state payload.
    This is called once when the user clicks the mascot to open the dashboard.
    launcher.py never queries SQLite directly — it only reads this dict.
    """
    from practice_mode import DB_PATH as PM_DB_PATH
    import sqlite3, os

    stats = {
        "vitals": {},
        "cp_stats": []
    }
    try:
        conn = sqlite3.connect(str(PM_DB_PATH), timeout=0.5)
        conn.row_factory = sqlite3.Row

        # ── System Vitals ──────────────────────────────────────────────────────
        db_size = os.path.getsize(str(PM_DB_PATH)) / (1024 * 1024)

        doc_count = conn.execute("SELECT COUNT(*) FROM knowledge_docs").fetchone()[0]
        mem_count = conn.execute("SELECT COUNT(*) FROM episodic_memories").fetchone()[0]

        # Top apps by how recently they were seen (knowledge_docs source)
        top_apps = conn.execute("""
            SELECT source_app as app_name, COUNT(*) as doc_count
            FROM knowledge_docs
            GROUP BY source_app
            ORDER BY doc_count DESC
            LIMIT 8
        """).fetchall()
        stats["vitals"] = {
            "db_size_mb": round(db_size, 2),
            "doc_count": doc_count,
            "memory_count": mem_count,
            "top_apps": [{"app": r["app_name"], "docs": r["doc_count"]} for r in top_apps],
        }

        # ── CP Stats — Level 1: Problem List ──────────────────────────────────
        problems = conn.execute("""
            SELECT problem_slug, platform,
                   COUNT(*) as session_count,
                   MAX(is_solved) as ever_solved,
                   MAX(last_seen) as last_seen
            FROM practice_sessions
            GROUP BY problem_slug
            ORDER BY last_seen DESC
        """).fetchall()
        cp_stats = []
        for prob in problems:
            slug = prob["problem_slug"]
            # Level 2: Sessions for this problem
            sessions = conn.execute("""
                SELECT id, last_seen, is_solved, detected_approach,
                       (SELECT COUNT(*) FROM practice_hints WHERE session_id = practice_sessions.id) as hint_count
                FROM practice_sessions
                WHERE problem_slug = ?
                ORDER BY last_seen DESC
            """, (slug,)).fetchall()
            sessions_data = []
            for sess in sessions:
                # Level 3: Hints for this session
                hints = conn.execute("""
                    SELECT hint_type, hint_text, code_snapshot, user_feedback, timestamp
                    FROM practice_hints
                    WHERE session_id = ?
                    ORDER BY timestamp ASC
                """, (sess["id"],)).fetchall()
                sessions_data.append({
                    "id": sess["id"],
                    "last_seen": sess["last_seen"],
                    "is_solved": sess["is_solved"],
                    "detected_approach": sess["detected_approach"] or "Unknown",
                    "hint_count": sess["hint_count"],
                    "hints": [
                        {
                            "hint_type": h["hint_type"],
                            "hint_text": h["hint_text"],
                            "code_snapshot": h["code_snapshot"] or "",
                            "user_feedback": h["user_feedback"],  # 1=helpful, 0=not, None=no feedback
                            "timestamp": h["timestamp"],
                        }
                        for h in hints
                    ]
                })
            cp_stats.append({
                "slug": slug,
                "platform": prob["platform"],
                "session_count": prob["session_count"],
                "ever_solved": bool(prob["ever_solved"]),
                "last_seen": prob["last_seen"],
                "sessions": sessions_data,
            })
        stats["cp_stats"] = cp_stats

        # ── Reshape into keys that dashboard.html actually reads ───────────────
        # ema_scores: list of {app, score} — we use knowledge_docs count as proxy
        stats["ema_scores"] = [
            {"app": r["app"], "score": r["docs"]}
            for r in stats["vitals"]["top_apps"]
        ]

        # markov_edges: not tracked yet — empty list renders graceful empty state
        stats["markov_edges"] = []

        # cp_history: flat list that problem list + overview use
        stats["cp_history"] = [
            {
                "slug":              p["slug"],
                "platform":          p["platform"],
                "is_solved":         p["ever_solved"],
                "stuck_count":       sum(s["hint_count"] for s in p["sessions"]),
                "detected_approach": (
                    p["sessions"][0]["detected_approach"]
                    if p["sessions"] else "Unknown"
                ),
                "sessions":          p["sessions"],
            }
            for p in cp_stats
        ]

        # strategy_heatmap: count of detected approaches across all problems
        heatmap = {}
        for p in cp_stats:
            approach = (
                p["sessions"][0]["detected_approach"]
                if p["sessions"] else None
            )
            if approach and approach not in ("Unknown", "NONE", None):
                heatmap[approach] = heatmap.get(approach, 0) + 1
        stats["strategy_heatmap"] = heatmap

        conn.close()
    except Exception as e:
        print(f"{_RED}[Dashboard] Stats query error: {e}{_RESET}")
    return stats

def pipe_listener_main(state, engine, embedder, mascot):
    handle = connect_to_pipe()
    print("[Python] Listening for events from C++...\n")

    reader = threading.Thread(
        target=_pipe_reader_daemon,
        args=(handle, state, engine, embedder, mascot),
        daemon=True  # Dies automatically if main thread exits
    )
    reader.start()

    # Main thread stays free — its only job is to catch KeyboardInterrupt
    try:
        while reader.is_alive():
            reader.join(timeout=0.5)  # Wakes every 500ms to check for signals
    except KeyboardInterrupt:
        print("\n[Python] KeyboardInterrupt received. Shutting down gracefully...", flush=True)
        _stop_event.set()       # Signal daemon to exit its loop cleanly
        win32file.CloseHandle(handle)
        sys.exit(0)

if __name__ == "__main__":
    ensure_models_downloaded()
    state = StateManager()
    engine = AIEngine()
    embedder = Embedder()
    flush_worker = FlushWorker(embedder, engine, state=state)
    flush_worker.start()
    cp_handler = CPEventHandler()

    # Spawn the always-on jugnuBug mascot
    mascot = MascotController()
    launcher_path = os.path.join(os.path.dirname(__file__), 'ui', 'launcher.py')
    mascot.spawn(launcher_path)
    mascot.start_output_reader(engine, embedder, launcher_path)

    # Wire the mascot into the cp_handler so it can update state
    cp_handler.mascot = mascot

    # Python is now a pure headless AI backend.
    # The listener loop runs synchronously on the main thread.
    pipe_listener_main(state, engine, embedder, mascot)

