#include <windows.h> // Fixed typo: 'windows.h' instead of 'window.h'
#include <iostream>
#include "monitor/win_monitor.h"
#include "monitor/file_watcher.h"
#include "server/ipc_server.h"
#include "server/flush_worker.h"
#include "db/db_handler.h"
#include "monitor/clipboard_manager.h"


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
    // Initialize the windows monitor. This installs the Win32 hook
    // so the OS starts notifying us whenever the user switches apps.
    Jugnu::WinMonitor::Init();

    // Start the background pipe listener thread
    Jugnu::IPCServer::Start();

    // Start the clipboard manager thread
    Jugnu::ClipboardManager::Start();

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
    // Clean up Clipboard Manager
    Jugnu::ClipboardManager::Stop();
    // Clean up DB
    Jugnu::DBHandler::Cleanup();

    return 0;
}