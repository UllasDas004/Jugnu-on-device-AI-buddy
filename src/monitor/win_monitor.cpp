#include "monitor/win_monitor.h"
#include "monitor/memory_manager.h"
#include "db/db_handler.h"
#include "server/ipc_server.h"
#include<psapi.h> // for getWindowTextA
// Definition of static member frequency                   
LARGE_INTEGER Jugnu::WinMonitor::frequency;
namespace Jugnu
{
    HWINEVENTHOOK WinMonitor::hook = nullptr;
    std::atomic<bool> WinMonitor::isRunning{false};
    HANDLE WinMonitor::hStuckThread = NULL;
    std::string WinMonitor::currentForegroundProcess = "";

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
            Jugnu::DBHandler::UpsertAppPath(finalProcessName, std::string(processPath));
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

        std::cout << "\033[90m[Debug] Foreground Switched: PID=" << windowPid << " | processName=" << processName << " | title=" << windowTitle << "\033[0m\n";

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
            std::cout << "[Debug] Ignored because ownPid == windowPid\n";
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
            std::cout << "[WinMonitor] Ignored Ghost Popup: User is AFK.\n";
            return;
        }
        
        // SAVE TO THE DATABASE
        Jugnu::DBHandler::LogAppSwitch(processName, windowTitle);
        Jugnu::MemoryManager::ProcessAppSwitch(processName, windowTitle);

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
        if(hStuckThread) {
            WaitForSingleObject(hStuckThread, 1000);
            CloseHandle(hStuckThread);
            hStuckThread = NULL;
        }

        if(hook)
        {
            UnhookWinEvent(hook);
            hook = nullptr;
            std::cout<<"[WinMonitor] Hook removed.\n";
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

        while(isRunning)
        {
            Sleep(1000); // Check every second

            LASTINPUTINFO lii;
            lii.cbSize = sizeof(LASTINPUTINFO);
            if (GetLastInputInfo(&lii)) {
                DWORD currentTick = GetTickCount(); 
                DWORD idleTime = currentTick - lii.dwTime;
                
                // If the user has been idle for > 5 seconds (5000 ms) for testing
                // AND they are currently staring at a coding app...
                if (idleTime > 180000)
                {
                    if (!hasTriggered)
                    {
                        std::cout << "\n\033[1;31m[StuckTimer]\033[0m User has been idle for 3 min. Notifying Python...\n";
                        std::string idlePayload = "{\"type\": \"USER_IDLE\", \"current_app\": \"" + currentForegroundProcess + "\"}";
                        Jugnu::IPCServer::SendMessageToPython(idlePayload);
                        hasTriggered = true; // Prevent spamming
                    }
                }
                else if(idleTime < 180000)
                    hasTriggered = false; // Reset when user moves the mouse again
            }
        }
        return 0;
    }
}