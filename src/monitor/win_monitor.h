#pragma once
#include <windows.h>
#include <string>
#include <iostream>
#include <atomic>

namespace Jugnu
{
    // Shared Kernel Event: signaled when user is in a Deep Work app, reset otherwise.
    // ScreenReader and StuckTimer both wait on this to hibernate when gaming/watching movies.
    inline HANDLE hDeepWorkEvent = NULL;
    
    class WinMonitor
    {
    public:
        // Initializes the monitor by registering the Win32 hook
        static void Init();
        
        // Cleans up and unregisters the hook before exiting
        static void Cleanup();

        // Helper: Extracts the underlying executable name (e.g., "Code.exe")
        static std::string GetProcessName(HWND hwnd);

        // Checks if the keyboard/mouse has been idle for 60 seconds
        static bool IsUserIdle();
        
    private:
        // Handle to the registered Windows Event Hook
        static HWINEVENTHOOK hook;
        static LARGE_INTEGER frequency; // For QueryPerformanceCounter

        // Helper function to get current time in milliseconds
        static inline double GetTimeMs();
        
        static std::atomic<bool> isRunning;
        static HANDLE hStuckThread;
        static std::string currentForegroundProcess;  // Raw — updated BEFORE filters (not safe for idle payload)
        static std::string lastMeaningfulApp;          // Safe — updated AFTER all filters pass
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
    };
}