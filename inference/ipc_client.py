import threading
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
from sentence_transformers import SentenceTransformer
import ollama

# Fix CUDA warmup crash on RTX 4050 / Turing+ with Ollama's Flash Attention PDL kernel
os.environ.setdefault("OLLAMA_FLASH_ATTENTION", "0")
def ensure_models_downloaded():
    print("[INIT] Checking AI Models... Please wait.")

    # 1. Check e5-small (Downloads to HuggingFace cache if missing)
    try:
        print("[INIT] Loading intfloat/e5-small-v2...")
        # This will automatically pull it if it doesn't exist locally
        model = SentenceTransformer('intfloat/e5-small-v2')
        print("[INIT] e5-small is ready!")
    
    except Exception as e:
        print(f"[FATAL] Failed to load e5-small: {e}")
        sys.exit(1)

    # 2. Check Gemma model via Ollama
    model_name = "gemma4:e2b"
    try:
        ollama.show(model_name)
        print(f"[INIT] {model_name} is ready!")
    except ollama.ResponseError:
        print(f"[INIT] Model {model_name} not found locally. Pulling from Ollama... This might take a few minutes depending on your internet.")

    try:
        # This blocks and downloads the multi-gigabyte model
        ollama.pull(model_name)
        print(f"[INIT] Successfully downloaded {model_name}!")
    except Exception as pull_err:
        print(f"[FATAL] Failed to pull Ollama model: {pull_err}")
        print("Make sure the Ollama background service is running!")
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
                print("Pipe not found. Is Jugni.exe running?")
                time.sleep(2)
            elif e.winerror == 231: # ERROR_PIPE_BUSY
                print("Pipe is busy. Retrying...")
                time.sleep(1)

def pipe_listener_main(state, engine, embedder):
    handle = connect_to_pipe()
    buffer = ""


    print("[Python] Listening for events from C++...\n")
    while True:
        try:
            # Read raw bytes from the named pipe
            result, data = win32file.ReadFile(handle, 4096)
            if result == 0:
                buffer += data.decode("utf-8")

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
                                        except Exception as e:
                                            print(f"\033[1;31m[Embedder] Could not read file: {e}\033[0m")
                                elif event_type == "USER_IDLE":
                                    current = payload.get('current_app', '').lower()
                                    if any(v in current for v in VETO_APPS):
                                        print("\033[90m[System] Veto app detected. Skipping.\033[0m", flush=True)
                                    elif state.was_recently_coding():
                                        print("\n\033[1;33m[System] User may be stuck! Launching notification flow...\033[0m", flush=True)
                                        threading.Thread(target=notification.trigger_flow, args=(state, engine, embedder), daemon=True).start()
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

    # Python is now a pure headless AI backend.
    # The listener loop runs synchronously on the main thread.
    pipe_listener_main(state, engine, embedder)

