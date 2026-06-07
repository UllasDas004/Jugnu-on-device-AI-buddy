#pragma once
#include <windows.h>
#include <string>
#include <iostream>
#include <atomic>

namespace Jugnu
{
    class WinMonitor
    {
    public:
        // Initializes the monitor by registering the Win32 hook
        static void Init();
        
        // Cleans up and unregisters the hook before exiting
        static void Cleanup();
        
    private:
        // Handle to the registered Windows Event Hook
        static HWINEVENTHOOK hook;
        
        static std::atomic<bool> isRunning;
        static HANDLE hStuckThread;
        static std::string currentForegroundProcess;
        static DWORD WINAPI StuckTimerThread(LPVOID lpParam);
        
        // The callback function that Windows OS calls whenever an event occurs
        static void CALLBACK WinEventProc(
            HWINEVENTHOOK hWinEventHook,
            DWORD event,
            HWND hwnd,
            LONG idObject,
            LONG idChild,
            DWORD dwEventThread,
            DWORD dwmsEventTime
        );
        
        // Helper: Extracts the text title of the window (e.g., "Jugnu - VS Code")
        static std::string GetWindowTextString(HWND hwnd);
        
        // Helper: Extracts the underlying executable name (e.g., "Code.exe")
        static std::string GetProcessName(HWND hwnd);

        // Checks if the keyboard/mouse has been idle for 60 seconds
        static bool IsUserIdle();
    };
}