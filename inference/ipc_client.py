import threading
import win32file
import win32pipe
import re
import pywintypes
import os
import sys
import time
import json
import notification
from state_manager import StateManager
from ai_engine import AIEngine
from embedder import Embedder
from flush_worker import (
    FlushWorker,
    _parse_cp_url,
    _get_or_create_practice_session,
    _compute_hint_level,
    _update_practice_session,
)
from sentence_transformers import SentenceTransformer
import ollama

# ANSI colors for terminal logging
_CYAN   = "\033[1;36m"
_GREEN  = "\033[1;32m"
_RED    = "\033[1;31m"
_YELLOW = "\033[1;33m"
_RESET  = "\033[0m"


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

def _has_meaningful_code(editor_text: str) -> bool:
    """
    Language-agnostic heuristic to check if user has started coding.
    Ignores comments, imports, scaffolding, and function/class declarations by structure,
    without blacklisting data types (int, vector, string, etc.) that are used in real code.
    """
    if not editor_text:
        return False
    
    lines = editor_text.splitlines()
    meaningful_lines = 0

    # Single-statement scaffolding that LeetCode / IDEs auto-fill
    _BOILERPLATE_EXACT = {"pass", "return 0;", "{", "}", "};", "class Solution {", "public:", "private:", "protected:"}
    _CONTROL_FLOW = ("if ", "if(", "else", "for ", "for(", "while ", "while(", "switch ", "switch(", "do ", "try", "catch", "return ")

    for line in lines:
        s = line.strip()
        # Skip blank lines, comments, imports, preprocessor directives
        if not s or s.startswith(("//", "#", "/*", "*", "import ", "using ", "#include", "package ", "from ")):
            continue
        # Skip exact boilerplate tokens
        if s in _BOILERPLATE_EXACT:
            continue
        # If line starts with control flow or return, it is ALWAYS meaningful code!
        if s.startswith(_CONTROL_FLOW):
            meaningful_lines += 1
            continue
        # If line ends with '{' or ':' (and is NOT control flow), it is a class/function declaration (e.g. `vector<int> twoSum(...) {` or `def twoSum(...):`) -> ignore!
        if s.endswith(("{", ":", "{ /", "{ //")):
            continue
        meaningful_lines += 1

    # At least 2 actual statements/expressions needed to be considered "coding"
    return meaningful_lines >= 2

def _idle_handler_background(state, engine, embedder, screen_context):
    """
    Runs in a background thread. Keeps the IPC listener free.
    Does: LLM query generation → KNN search → situation detection → notification.
    """
    # Extract URL and Title preserved by state_manager
    url_match = re.search(r'\[URL:\s*(https?://[^\]]+)\]', screen_context)
    active_url = url_match.group(1) if url_match else None

    title_match = re.search(r'\[TITLE:\s*([^\]]+)\]', screen_context)
    active_title = title_match.group(1) if title_match else ""
    
    cp_info = _parse_cp_url(active_url)
    is_cp_session = cp_info is not None

    if is_cp_session:
        # Build KNN search query directly from window title and UIA page content!
        # This mirrors the exact anchor used when saving to vector DB, ensuring high cosine similarity
        # to the current problem without calling Ollama or risking hallucinations.
        page_content = ""
        if "--- Page Content ---" in screen_context:
            page_content = screen_context.split("--- Page Content ---")[1].strip()[:400]
        
        anchor_title = active_title if active_title else (f"{cp_info['platform'].capitalize()}: {cp_info['slug']}")
        search_query = f"{anchor_title}. {page_content}".strip() if (anchor_title or page_content) else anchor_title
        print(f"{_CYAN}[IPC-BG] CP Session — using deterministic title+content anchor for KNN: '{search_query[:80]}...'{_RESET}")
    else:
        print(f"{_YELLOW}[IPC-BG] Generating search query...{_RESET}")
        search_query = engine.generate_search_query(screen_context)
        print(f"{_YELLOW}[IPC-BG] KNN Query: '{search_query}'{_RESET}")

    # We search across ALL CP problems ("cp") so that similar problems or algorithms can be retrieved!
    # Because search_query is now anchored to actual problem title and text (e.g. "Two Sum - LeetCode. Given an array..."),
    # KNN will naturally return the current problem as #1 (distance ~0.0), and similar algorithmic problems as #2 and #3.
    required_tag = "cp" if is_cp_session else None

    knowledge_docs = embedder.search_knowledge_docs(search_query, limit=3, required_tag=required_tag)
    context_chunks  = []
    sources         = []
    situation_type  = "NO_MEMORY"

    if is_cp_session:
        # Check if user has written actual code vs staring at default boilerplate
        editor_section = ""
        if "--- Editor Content ---" in screen_context:
            after_editor = screen_context.split("--- Editor Content ---")[1]
            editor_section = after_editor.split("--- Page Content ---")[0].strip() if "--- Page Content ---" in after_editor else after_editor.strip()
        
        # Use language-agnostic attempt detector instead of fragile character counting
        if not _has_meaningful_code(editor_section):
            situation_type = "CP_READING"  # Reading / understanding the problem
            print(f"{_GREEN}[IPC-BG] Situation: CP_READING (Template/Boilerplate only){_RESET}")
        else:
            situation_type = "CP_STUCK"    # Stuck on implementation / bugs
            print(f"{_GREEN}[IPC-BG] Situation: CP_STUCK (Active code attempt){_RESET}")

    if knowledge_docs:
        top           = knowledge_docs[0]
        capture_count = top.get('capture_count', 1)
        source_type   = top.get('source_type', '')
        code_snippet  = top.get('code_snippet', '')
        
        # Only compute general situations if we aren't already in a CP session!
        if not is_cp_session:
            if capture_count >= 4:
                situation_type = "REPEATED_STRUGGLE"
            elif source_type == 'ide':
                situation_type = "STUCK_ON_OWN_CODE"        # always — user was writing code
            elif source_type == 'browser' and code_snippet:
                situation_type = "STUCK_ON_OWN_CODE"        # browser with code = practice problem
            elif source_type == 'browser':
                situation_type = "READING_NEW_MATERIAL"     # browser, no code = pure docs
            else:
                situation_type = "GENERAL"

        sources = [doc['topic'] for doc in knowledge_docs]
        print(f"{_GREEN}[IPC-BG] Situation: {situation_type} | Docs: {len(knowledge_docs)}{_RESET}")
    
    else:
        # Fallback: episodic memories
        memories = embedder.semantic_search(search_query, limit=3)
        if memories:
            context_chunks = [m["snippet"] for m in memories]
            sources        = ["past session memory"]
            situation_type = "GENERAL"
            print(f"{_GREEN}[IPC-BG] Falling back to {len(memories)} episodic memories.{_RESET}")
        else:
            print(f"{_YELLOW}[IPC-BG] No memory found. Notification will use general insight.{_RESET}")
        
    # ── PRACTICE MODE CHECK ────────────────────────────────────────────────
    # Check both CP_STUCK (active coding) and STUCK_ON_OWN_CODE (idle timer fallback)
    if (situation_type in ("CP_STUCK", "STUCK_ON_OWN_CODE") and knowledge_docs):
        top_doc = knowledge_docs[0]
        top_tags = top_doc.get("tags", [])
        is_practice_mode = any(t in top_tags for t in ("solved", "accepted", "fresh_submission"))

        if is_practice_mode:
            print(f"{_GREEN}[IPC-BG] PRACTICE MODE — routing to progressive hint system.{_RESET}")

            # Derive platform & slug from the DB topic (e.g. "Leetcode: two-sum")
            # This completely removes the dependency on live URL extraction during IDLE!
            topic = top_doc.get("topic", "")
            slug, platform = "unknown", "unknown"
            for pf in ("leetcode", "codeforces", "codechef", "atcoder"):
                if pf in topic.lower():
                    platform = pf
                    parts = topic.split(":", 1)
                    slug = parts[1].strip().lower() if len(parts) > 1 else topic
                    break
            
            if slug == "unknown" and cp_info:
                # Fallback to URL info if topic parsing failed but URL exists
                slug = cp_info.get("slug", "unknown")
                platform = cp_info.get("platform", "unknown")

            current_code = top_doc.get("code_snippet", "") or ""
            
            if not current_code.strip():
                print(f"{_YELLOW}[IPC-BG] Practice mode: no code snapshot yet. Skipping hint.{_RESET}")
            else:
                # Step 1: Get or create session (fast SQLite lookup)
                session = _get_or_create_practice_session(slug, platform)

                if session:
                    # Step 2: Decide hint level (code diff + approach change check)
                    hint_level, should_advance, similarity = _compute_hint_level(
                        session, current_code, ""
                    )
                    # Step 3: Detect approach ONLY if code changed significantly or it's the first time
                    if similarity >= 0.70 and session.get("detected_approach"):
                        detected_approach = session.get("detected_approach")
                        print(f"{_CYAN}[IPC-BG] Code unchanged. Reusing approach: {detected_approach}{_RESET}")
                    else:
                        detected_approach = engine.detect_approach(current_code)
                        print(f"{_CYAN}[IPC-BG] New approach detected: {detected_approach}{_RESET}")
                    # Step 4: Generate the progressive hint
                    hint_text = engine.generate_practice_hint(
                        problem_content   = top_doc.get("content", ""),
                        problem_notes     = top_doc.get("notes", ""),
                        current_code      = current_code,
                        hint_level        = hint_level,
                        last_hint         = session.get("last_hint_text") or "",
                        detected_approach = detected_approach,
                    )
                    # Step 5: Display via normal notification flow
                    # The hint is pre-generated — pass it in context_chunks so
                    # notification.py uses the 'context_chunks' path directly
                    # without calling Gemma again.
                    practice_sources = [
                        f"{platform.capitalize()}: {slug} "
                        f"(Practice — Hint Level {hint_level}/3)"
                    ]
                    notification.trigger_flow(
                        state, engine, embedder,
                        search_query   = search_query,
                        context_chunks = [hint_text],
                        knowledge_docs = [],
                        sources        = practice_sources,
                        screen_context = screen_context,
                        situation_type = "CP_STUCK",
                    )
                    # Step 6: Persist session state
                    _update_practice_session(
                        session_id        = session["id"],
                        hint_level        = hint_level,
                        hint_text         = hint_text,
                        current_code      = current_code,
                        detected_approach = detected_approach,
                        is_solved         = False,
                    )
            return   # always return — don't fall through to generic flow
    # ── NORMAL FLOW (CP_READING, unsolved problems, non-CP sessions) ───────
    notification.trigger_flow(
        state, engine, embedder,
        search_query   = search_query,
        context_chunks = context_chunks,
        knowledge_docs = knowledge_docs,
        sources        = sources,
        screen_context = screen_context,
        situation_type = situation_type,
    )


def _pipe_reader_daemon(handle, state, engine, embedder):
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
                                        screen_context_snapshot = state.generate_prompt_context(embedder=None, target_app = idle_app)
                                        threading.Thread(
                                            target=_idle_handler_background,
                                            args=(state, engine, embedder, screen_context_snapshot),
                                            daemon=True
                                        ).start()
                                    else:
                                        print("\033[90m[System] No recent coding context. Skipping.\033[0m", flush=True)

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


def pipe_listener_main(state, engine, embedder):
    handle = connect_to_pipe()
    print("[Python] Listening for events from C++...\n")

    reader = threading.Thread(
        target=_pipe_reader_daemon,
        args=(handle, state, engine, embedder),
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

    # Python is now a pure headless AI backend.
    # The listener loop runs synchronously on the main thread.
    pipe_listener_main(state, engine, embedder)

