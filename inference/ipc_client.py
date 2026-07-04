import threading
from socket import socket
import win32file
import pywintypes
import os
import sys
import time
import json
import notification
from state_manager import StateManager
from ai_engine import AIEngine
from embedder import Embedder
from flush_worker import FlushWorker
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
        socket.setdefaulttimeout(3)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
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

CODING_APPS = ["code", "ide", "antigravity", "pwsh", "terminal",
               "devenv", "vim", "nvim", "fleet", "clion", "pycharm"]
OS_NOISE    = ["explorer", "shellexperiencehost", "searchapp",
               "startmenuexperiencehost", "applicationframehost",
               "textinputhost"]
VETO_APPS   = ["netflix", "vlc", "steam", "spotify", "discord",
               "zoom", "ms-teams", "teams", "slack", "whatsapp"]

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
            if e.winerror == 2: # ERROR_FILE_NO_FOUND
                print("Pipe not found. Is Jugnu.exe running?")
                time.sleep(2)
            elif e.winerror == 231: # ERROR_PIPE_BUSY
                print("Pipe is busy. Retrying...")
                time.sleep(1)

def _synthesize_and_save_file(engine, embedder, app_name, filepath, code_text):
    """
    Background thread: chunks a saved code file, extracts technical knowledge
    with Gemma, synthesizes into one OKF knowledge_doc, and saves it.
    Only runs for files under 8000 chars to avoid VRAM overruns.
    """
    MAX_FILE_CHARS = 8000
    CHUNK_SIZE = 500

    if len(code_text) > MAX_FILE_CHARS:
        print(f"{_YELLOW}[OKF] File too large for synthesis ({len(code_text)} chars). Episodic-only.{_RESET}")
        return
    
    from flush_worker import _chunk_text, MIN_CHUNK_WORDS
    chunks = _chunk_text(code_text, CHUNK_SIZE)
    all_extractions = []
    prev_extracted = ""

    for chunk in chunks:
        if len(chunk.split()) < MIN_CHUNK_WORDS:
            continue
        extracted = engine.extract_ocr_chunk(chunk, prev_context=prev_extracted)
        if extracted and len(extracted.split()) >= MIN_CHUNK_WORDS:
            all_extractions.append(extracted)
            prev_extracted = extracted
        else:
            prev_extracted = chunk[:100]

    if not all_extractions:
        print(f"{_YELLOW}[OKF] No useful knowledge extracted from {os.path.basename(filepath)}.{_RESET}")
        return

    doc_json = engine.synthesize_ocr_extractions(all_extractions)
    if doc_json:
        saved = embedder.save_knowledge_doc(app_name, doc_json, engine)
        if saved:
            print(f"{_GREEN}[OKF] Knowledge doc saved from FILE_SAVED: {os.path.basename(filepath)}{_RESET}")
        else:
            print(f"{_YELLOW}[OKF] Synthesis failed for {os.path.basename(filepath)} — episodic only.{_RESET}")

def pipe_listener_main(state, engine, embedder):
    handle = connect_to_pipe()
    buffer = ""


    print("[Python] Listening for events from C++...\n")
    while True:
        try:
            # Read raw bytes from the named pipe
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
                                    if not any(n in app.lower() for n in OS_NOISE):
                                        if any(c in app.lower() for c in CODING_APPS):
                                            state.set_last_coding_app(app)
                                elif event_type == 'CLIPBOARD':
                                    text = payload.get('text', '')
                                    state.update_clipboard(text)
                                    # Save to semantic memory if it's substantial text
                                    if text and len(text.strip()) > 20:
                                        threading.Thread(
                                            target = embedder.save_memory,
                                            args = (state.current_app or 'unknown',
                                                    state.current_app or 'clipboard',
                                                    text),
                                            kwargs = {'file_path': None},
                                            daemon=True
                                        ).start()
                                    # If it looks like a code snippet, synthesize into OKF
                                    if text and len(text.strip()) > 100:
                                        threading.Thread(
                                            target=_synthesize_and_save_file,
                                            args=(engine, embedder,
                                                  state.current_app or 'clipboard',
                                                  'clipboard', text),
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
                                                target = embedder.save_memory,
                                                args = (state.current_app or 'unknown',
                                                        state.current_app or 'unknown',
                                                        code_text),
                                                kwargs = {'file_path': filepath},
                                                daemon=True
                                            ).start()

                                            threading.Thread(
                                                target=_synthesize_and_save_file,
                                                args=(engine, embedder,
                                                      state.current_app or 'unknown',
                                                      filepath, code_text),
                                                daemon=True
                                            ).start()
                                        except Exception as e:
                                            print(f"\033[1;31m[Embedder] Could not read file: {e}\033[0m")
                                elif event_type == "USER_IDLE":
                                    current_app = payload.get('current_app', '').lower()
                                    
                                    # Check VETO apps first
                                    if any(v in current_app for v in VETO_APPS):
                                        print(f"\033[90m[System] Veto app ({current_app}) detected. Skipping RAG.\033[0m", flush=True)
                                    elif state.was_recently_coding():

                                        # Step 1: Gemma reads screen context and generates a focused search query
                                        # (This is a cheap, fast LLM call — just 30 tokens)
                                        screen_context = state.generate_prompt_context(embedder=None)
                                        search_query = engine.generate_search_query(screen_context)
                                        print(f"{_YELLOW}[IPC] KNN Query: '{search_query}'{_RESET}")

                                        # Step 2: Run KNN search ONLY (fast vector math, no GPU inference)
                                        # We store the results and pass them to notification.
                                        # Gemma will generate the actual answer ONLY if user clicks Y.
                                        context_chunks = []
                                        sources        = []

                                        knowledge_results = embedder.search_knowledge_docs(search_query, limit=3)
                                        if knowledge_results:
                                            for doc in knowledge_results:
                                                sources.append(doc['topic'])
                                                context_chunks.append(
                                                    f"[Topic: {doc['topic']} | Seen {doc['capture_count']}x]\n{doc['content']}"
                                                )
                                            print(f"{_GREEN}[IPC] Found {len(sources)} knowledge docs. Spawning notification...{_RESET}")
                                        else:
                                            memories = embedder.semantic_search(search_query, limit=3)
                                            if memories:
                                                context_chunks = [m["snippet"] for m in memories]
                                                sources = ["past session memory"]
                                                print(f"{_GREEN}[IPC] Found {len(memories)} episodic memories. Spawning notification...{_RESET}")
                                            else:
                                                print(f"{_YELLOW}[IPC] No memory found. Notification will use general insight.{_RESET}")

                                        # Spawn the notification window — it will run Gemma AFTER user clicks Y
                                        threading.Thread(
                                            target=notification.trigger_flow,
                                            args=(state, engine, embedder),
                                            kwargs={
                                                "search_query":  search_query,
                                                "context_chunks": context_chunks,
                                                "sources":        sources,
                                                "screen_context": screen_context,
                                            },
                                            daemon=True
                                        ).start()
                                    else:
                                        print("\033[90m[System] No recent coding context. Skipping.\033[0m", flush=True)
                            except json.JSONDecodeError:
                                print(f"\033[1;31m[Error] Failed to decode JSON:\033[0m {msg}", flush=True)
                    
                    #Keep any remainder for the next loop
                    buffer = messages[-1]

        except pywintypes.error as e:
            if e.winerror == 109: # ERROR_BROKEN_PIPE
                print("\n[Python] C++ engine disconnected. Reconnecting...", flush=True)
                win32file.CloseHandle(handle)
                handle = connect_to_pipe()
            else:
                print(f"[Python] Pipe read error: {e}", flush=True)
                break
        except KeyboardInterrupt:
            print("\n[Python] KeyboardInterrupt received. Shutting down gracefully...", flush=True)
            win32file.CloseHandle(handle)
            sys.exit(0)
        except Exception as e:
            print(f"\n[Python] Unexpected error: {e}", flush=True)
            break

if __name__ == "__main__":
    ensure_models_downloaded()
    state = StateManager()
    engine = AIEngine()
    embedder = Embedder()
    flush_worker = FlushWorker(embedder, engine)
    flush_worker.start()

    # Python is now a pure headless AI backend.
    # The listener loop runs synchronously on the main thread.
    pipe_listener_main(state, engine, embedder)

