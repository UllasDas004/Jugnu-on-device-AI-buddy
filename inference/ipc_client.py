import webview
import win32file
import pywintypes
import time
import json
import threading
import webview
import notification
from state_manager import StateManager
from ai_engine import AIEngine

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

def pipe_listener_main(state, engine):
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
                                    state.update_clipboard(payload.get('text'))
                                elif event_type == "FILE_SAVED":
                                    state.update_file(payload.get('file'))
                                elif event_type == "USER_IDLE":
                                    current = payload.get('current_app', '').lower()
                                    if any(v in current for v in VETO_APPS):
                                        print("\033[90m[System] Veto app detected. Skipping.\033[0m", flush=True)
                                    elif state.was_recently_coding():
                                        print("\n\033[1;33m[System] User may be stuck! Launching notification flow...\033[0m", flush=True)

                                        threading.Thread(
                                            target=notification.trigger_flow,
                                            args=(state, engine),
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

if __name__ == "__main__":
    state = StateManager()
    engine = AIEngine()

    webview.create_window("Jugnu Background Service", hidden = True)
    webview.start(pipe_listener_main, (state, engine), debug = False)

