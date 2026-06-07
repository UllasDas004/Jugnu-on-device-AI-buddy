#include <windows.h> // Fixed typo: 'windows.h' instead of 'window.h'
#include <iostream>
#include "monitor/win_monitor.h"
#include "monitor/file_watcher.h"
#include "server/ipc_server.h"
#include "server/flush_worker.h"
#include "db/db_handler.h"

// The Window Procedure for our invisible Message-Only Window
LRESULT CALLBACK ClipboardWindowProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam)
{
    if(msg == WM_CLIPBOARDUPDATE)
    {
        // Step 1: Open the OS Clipboard
        if(OpenClipboard(hwnd))
        {
            // Step 2: Grab the copied text (CF_TEXT)
            HANDLE hData = GetClipboardData(CF_TEXT);
            if(hData)
            {
                char* text = static_cast<char*>(GlobalLock(hData));
                if(text)
                {
                    std::string copiedText(text);
                    GlobalUnlock(hData);
                    
                    // Step 3: Only interupt if it's a large block of text (e.g., > 150 chars)
                    if(copiedText.length() > 150)
                    {
                        std::cout<<"[Clipboard] Intercepted massive copy! Sending to AI...\n";

                        // We will fomrat this as JSON for Python later, but for now just send the raw text!
                        std::string payload = "{\"type\": \"CLIPBOARD\", \"text\": \"" + copiedText.substr(0, 50) + "...\"}";

                        Jugnu::IPCServer::SendMessageToPython(payload);
                    }
                }
            }
            CloseClipboard();
        }
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}


int main()
{
    // Enable ANSI colors for Windows Terminal
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD dwMode = 0;
    GetConsoleMode(hOut, &dwMode);
    SetConsoleMode(hOut, dwMode | ENABLE_VIRTUAL_TERMINAL_PROCESSING);

    std::cout << "\033[1;36m======================================\033[0m\n";
    std::cout << "\033[1;36m       Jugnu C++ Engine Skeleton      \033[0m\n";
    std::cout << "\033[1;36m======================================\033[0m\n\n";

    // Initialize the Database first
    if(!Jugnu::DBHandler::Init())
    {
        std::cerr<<"Failed to initialize database. Existing.\n";
        return 1;
    }

    // 1. Register a dummy Window class
    WNDCLASS wc = {0}; // Initialize all fields to 0
    wc.lpfnWndProc = ClipboardWindowProc; // Assign the callback, telling windows to call this function when something happens.
    wc.hInstance = GetModuleHandle(NULL); // HINSTANCE is a handle to the instance of the application
    wc.lpszClassName = "JugnuClipboardListener"; // Class name
    RegisterClass(&wc);

    // 2. Create the Message-Only Window (HWND_MESSAGE makes it completely invisible)
    HWND hwndHidden = CreateWindowEx(
        0, "JugnuClipboardListener", "JugnuDummy",
        0, 0, 0, 0, 0, HWND_MESSAGE, NULL, wc.hInstance, NULL
    );

    // 3. Tell Windows to route all Ctrl+c / Copy events to this invisible window!
    AddClipboardFormatListener(hwndHidden);

    // Initialize the windows monitor. This installs the Win32 hook
    // so the OS starts notifying us whenever the user switches apps.
    Jugnu::WinMonitor::Init();

    // Start the background pipe listener thread
    Jugnu::IPCServer::Start();

    // Start the flush worker thread
    Jugnu::FlushWorker::Start();

    // Start watching the dynamic development folder!
    char cwd[MAX_PATH];
    if (GetCurrentDirectoryA(MAX_PATH, cwd)) {
        std::string projectPath = std::string(cwd);
        std::cout << "\033[1;32m[System]\033[0m Auto-detected coding folder: \033[4m" << projectPath << "\033[0m\n";
        Jugnu::FileWatcher::Start(projectPath);
    }

    std::cout << "\033[1;32m[System]\033[0m \033[1;37mRunning. Switch to different apps to test.\033[0m\n";
    std::cout << "\033[1;32m[System]\033[0m \033[1;37mPress Ctrl+C to exit.\033[0m\n\n";

    // Standard Win32 Message Loop. 
    // This is required for WinEventHook callbacks to work. 
    // It keeps the application alive in the background at 0% CPU,
    // waking up only when Windows sends an event.
    MSG msg;
    while(GetMessage(&msg, NULL, 0, 0))
    {
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }

    // Clean up the hook before exiting
    Jugnu::WinMonitor::Cleanup();
    // Clean up IPC Server
    Jugnu::IPCServer::Stop();
    // Clean up Flush Worker
    Jugnu::FlushWorker::Stop();
    // Clean up File Watcher
    Jugnu::FileWatcher::Stop();
    // Clean up DB
    Jugnu::DBHandler::Cleanup();

    RemoveClipboardFormatListener(hwndHidden);
    return 0;
}