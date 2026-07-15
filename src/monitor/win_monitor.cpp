#include "monitor/win_monitor.h"
#include "monitor/memory_manager.h"
#include "db/db_handler.h"
#include "server/ipc_server.h"
#include <psapi.h> // for getWindowTextA
#include <unordered_set>

// Definition of static member frequency                   
LARGE_INTEGER Jugnu::WinMonitor::frequency;
namespace Jugnu
{
    HWINEVENTHOOK WinMonitor::hook = nullptr;
    std::atomic<bool> WinMonitor::isRunning{false};
    HANDLE WinMonitor::hStuckThread = NULL;
    std::string WinMonitor::currentForegroundProcess = "";
    std::string WinMonitor::lastMeaningfulApp = "";
    
    // --- Deep Work Whitelist ---
    // Shared between the foreground hook (to gate UIA extraction) 
    // and the StuckTimer (to prevent notifications during games/movies).
    static const std::unordered_set<std::string> DeepWorkWhitelist = {
        "code.exe", "devenv.exe", "clion64.exe", "idea64.exe", 
        "pycharm64.exe", "rider64.exe", "webstorm64.exe", 
        "sublime_text.exe", "notepad++.exe", "WindowsTerminal.exe", 
        "pwsh.exe", "chrome.exe", "Antigravity IDE.exe"
    };

    std::string WinMonitor::GetWindowTextString(HWND hwnd)
    {
        char title[1024];
        // Extracts the actual text title from the window handle
        GetWindowTextA(hwnd, title, sizeof(title));
        return std::string(title);
    }

    std::string WinMonitor::GetProcessName(HWND hwnd)
    {
        DWORD pid = 0;
        // Step 1: Get the Process ID (PID) from the Window Handle
        GetWindowThreadProcessId(hwnd, &pid);
        if(pid == 0) return "Unknown";

        // Step 2: Open the process memory so we can read its executable name
        HANDLE hProcess = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, pid);
        if(!hProcess) return "Unknown";

        char processName[MAX_PATH];
        std::string finalProcessName = "Unknown";
        // Step 3: Extract the actual executable name (e.g., "code.exe", "chrome.exe")
        if(GetModuleBaseNameA(hProcess, NULL, processName, MAX_PATH))
        {
            finalProcessName = std::string(processName);

            // THE PREFETCH EXTRACTOR: Grab the absolute C:\ path!
            char processPath[MAX_PATH];
            if(GetModuleFileNameExA(hProcess, NULL, processPath, MAX_PATH))
            {
                Jugnu::DBHandler::UpsertAppPath(finalProcessName, std::string(processPath));
            }
        }
        
        CloseHandle(hProcess);
        return finalProcessName;
    }

    void CALLBACK WinMonitor::WinEventProc(
            HWINEVENTHOOK hWinEventHook,
            DWORD event,
            HWND hwnd,
            LONG idObject,
            LONG idChild,
            DWORD dwEventThread,
            DWORD dwmsEventTime
        )
    {
        double startTime = GetTimeMs();

        // We only care about the main Window changing, not sub-elements or cursors
        if(idObject != OBJID_WINDOW) return;

        std::string processName = GetProcessName(hwnd);
        std::string windowTitle = GetWindowTextString(hwnd);

        // Save current foreground process for the Stuck Timer thread
        currentForegroundProcess = processName;

        DWORD ownPid = GetCurrentProcessId();
        DWORD windowPid;
        GetWindowThreadProcessId(hwnd, &windowPid);

        std::cout << "\033[1;36m[System]\033[0m Foreground Switched: PID=" << windowPid << " | processName=\033[1;32m" << processName << "\033[0m | title=" << windowTitle << "\n";

        // Ignore Explorer.EXE during transient states (Alt-Tab task switcher, empty titles)
        if(processName == "Explorer.EXE" || processName == "explorer.exe")
        {
            if(windowTitle == "Task Switching" || windowTitle.empty())
            {
                std::cout << "\033[90m[WinMonitor]\033[0m Ignoring Explorer.EXE transient state: \""
                            << windowTitle << "\"\033[0m\n";
                return;
            }
            if(windowTitle.find("File Explorer") == std::string::npos)
            {
                std::cout << "\033[90m[WinMonitor]\033[0m Ignoring non-file Explorer window: \""
                        << windowTitle << "\"\033[0m\n";
                return;
            }
        }
        
        if (ownPid == windowPid) {
            std::cout << "\033[90m[Debug] Ignored because ownPid == windowPid\033[0m\n";
            return;
        }

        // Ignore invisible OS panels, taskbars, and ghost windows
        if(windowTitle.empty()) {
            std::cout << "\033[90m[Debug] Ignored because windowTitle is empty\033[0m\n";
            return;
        }

        // Anti-Idle Ghost Popup Trap!
        // If a window stole focus while the user was away, ignore it!
        if(IsUserIdle())
        {
            std::cout << "\033[1;36m[WinMonitor]\033[0m Ignored Ghost Popup: User is AFK.\n";
            return;
        }
        
        // SAVE TO THE DATABASE
        Jugnu::DBHandler::LogAppSwitch(processName, windowTitle);
        Jugnu::MemoryManager::ProcessAppSwitch(processName, windowTitle);

        
        if(DeepWorkWhitelist.find(processName) == DeepWorkWhitelist.end())
        {
            // Non-work app → put both threads to sleep
            if(hDeepWorkEvent) ResetEvent(hDeepWorkEvent);
            std::cout << "\033[90m[Jugnu]\033[0m \'" << processName << "\' is outside the focus zone — background threads entering standby.\n";
            return;
        }

        // Deep Work app → wake up both threads
        if(hDeepWorkEvent) SetEvent(hDeepWorkEvent);
        std::cout << "\033[1;32m[Jugnu]\033[0m Focus zone active: \'" << processName << "\' — monitoring threads are live.\n";

        // All filters passed — this is a real user-focused Deep Work app. Safe to use in idle payload.
        lastMeaningfulApp = processName;

        // GENERATE JSON AND BROADCAST TO PYTHON
        std::string payload = Jugnu::MemoryManager::GenerateContextJSON(processName);

        double ipcStart = GetTimeMs();
        Jugnu::IPCServer::SendMessageToPython(payload);
        double ipcEnd = GetTimeMs();

        if(ipcEnd - ipcStart > 0.5)
        {
            std::cout << "\033[1;33m[WinMonitor]\033[0m "
                    << "IPC send took " << (ipcEnd - ipcStart) << " ms" << std::endl;
        }

        double endTime = GetTimeMs();
        double durationMs = endTime - startTime;

        if(durationMs > 1.0)
        {
            std::cout << "\033[1;33m[WinMonitor]\033[0m "
                    << "Slow event processed in " << durationMs << " ms "
                    << "(app: " << processName << ")" <<
                std::endl;
        }
    }

    void WinMonitor::Init()
    {
        // Create a manual-reset event, initially non-signaled (threads start suspended).
        // Manual-reset means ALL waiting threads wake up when SetEvent() is called, not just one.
        hDeepWorkEvent = CreateEvent(NULL, TRUE, FALSE, NULL);

        std::cout << "\033[1;36m[WinMonitor]\033[0m Installing EVENT_SYSTEM_FOREGROUND hook...\n";
        
        // Grab the initial window state in case the user started Jugnu inside an integrated terminal!
        HWND hwnd = GetForegroundWindow();
        if(hwnd) {
            currentForegroundProcess = GetProcessName(hwnd);
        }

        isRunning = true;
        hStuckThread = CreateThread(NULL, 0, StuckTimerThread, NULL, 0, NULL);
        
        // This tells Windows to call WinEventProc whenever the foreground window changes.
        // WINEVENT_SKIPOWNPROCESS prevents Jugnu from tracking itself (Fix for Trap F-1).
        hook = SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND,
            EVENT_SYSTEM_FOREGROUND,
            NULL,
            WinEventProc,
            0,
            0,
            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        );

        if(!hook) std::cerr<<"\033[1;31m[WinMonitor]\033[0m FAILED to install hook!\n";
        else std::cout<<"\033[1;36m[WinMonitor]\033[0m Hook installed successfully.\n";

        // Initialize the performances counter frequency
        QueryPerformanceFrequency(&WinMonitor::frequency);
    }

    void WinMonitor::Cleanup()
    {
        isRunning = false;

        // CRITICAL ORDER: Signal the event FIRST so blocked threads can unblock,
        // check isRunning=false, and exit. THEN we wait for the thread to finish.
        // If we wait BEFORE signaling, the thread blocks forever on WaitForSingleObject.
        if(hDeepWorkEvent)
            SetEvent(hDeepWorkEvent);

        if(hStuckThread) {
            WaitForSingleObject(hStuckThread, 2000);
            CloseHandle(hStuckThread);
            hStuckThread = NULL;
        }

        if(hook)
        {
            UnhookWinEvent(hook);
            hook = nullptr;
            std::cout<<"[WinMonitor] Hook removed.\n";
        }

        if(hDeepWorkEvent)
        {
            CloseHandle(hDeepWorkEvent);
            hDeepWorkEvent = NULL;
        }
    }

    bool WinMonitor::IsUserIdle()
    {
        LASTINPUTINFO lii;
        lii.cbSize = sizeof(LASTINPUTINFO);

        if(GetLastInputInfo(&lii))
        {
            // GetTickCount() is the OS uptime in milliseconds
            DWORD currentTick = GetTickCount();
            DWORD idleTime = currentTick - lii.dwTime;
            return idleTime > 60000;
        }
        return false;
    }

    double WinMonitor::GetTimeMs()
    {
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        return (double)now.QuadPart / (double)frequency.QuadPart * 1000.0;
    }

    DWORD WINAPI WinMonitor::StuckTimerThread(LPVOID lpParam)
    {
        bool hasTriggered = false;
        bool wasHibernating = true; // Track state to avoid spamming prints

        while(isRunning)
        {
            // PHASE 1: Hibernate. Sleep infinitely while user is in a game/movie.
            // When WinEventProc sees a Deep Work app, it calls SetEvent() to wake us.
            if(wasHibernating)
                std::cout << "\033[90m[StuckTimer]\033[0m Standby — waiting for a focus session to begin.\n";
            WaitForSingleObject(hDeepWorkEvent, INFINITE);
            if(!isRunning) break;   // Cleanup() called SetEvent to unblock us for exit

            if(wasHibernating)
            {
                std::cout << "\033[1;32m[StuckTimer]\033[0m Focus session detected — idle guard is active.\n";
                wasHibernating = false;
            }

            // PHASE 2: User is in a Deep Work app. Use dynamic math — no fixed polling.
            LASTINPUTINFO lii;
            lii.cbSize = sizeof(LASTINPUTINFO);
            if(!GetLastInputInfo(&lii))
            {
                Sleep(10000);   // Failsafe: prevent divide-by-zero / spinloop if API fails
                continue;
            }
            DWORD currentTick = GetTickCount(); 
            DWORD idleTime = currentTick - lii.dwTime;
            
            if (idleTime > 180000)
            {
                // Hit 3 minutes! Fire the stuck notification (only once per idle session).
                if (!hasTriggered)
                {
                    std::cout << "\n\033[1;31m[StuckTimer]\033[0m No activity for 3 minutes. Sending focus nudge to Python...\n";
                    const std::string& idleApp = lastMeaningfulApp.empty() ? currentForegroundProcess : lastMeaningfulApp;
                    std::string idlePayload = "{\"type\": \"USER_IDLE\", \"current_app\": \"" + idleApp + "\"}";
                    Jugnu::IPCServer::SendMessageToPython(idlePayload);
                    hasTriggered = true;
                }
                // Stay in a slow 10s loop while they remain stuck (don't spin-lock)
                Sleep(10000);
            }
            else
            {
                // User is active. Check if event was reset (they switched to a non-work app)
                DWORD eventState = WaitForSingleObject(hDeepWorkEvent, 0);
                if(eventState == WAIT_TIMEOUT)
                {
                    // Event was reset — they switched to a game/movie.
                    std::cout << "\033[90m[StuckTimer]\033[0m Left the focus zone — idle guard entering standby.\n";
                    wasHibernating = true;
                    hasTriggered = false;
                    continue; // Loop back to WaitForSingleObject(INFINITE)
                }

                hasTriggered = false;
                DWORD timeRemaining = 180000 - idleTime;
                std::cout << "\033[90m[StuckTimer]\033[0m User is active. Idle guard sleeping for "
                          << (timeRemaining / 1000) << "s.\n";
                Sleep(timeRemaining);
            }
        }
        return 0;
    }
}