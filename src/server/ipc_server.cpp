#include "ipc_server.h"
#include <iostream>
// <windows.h> is already included transitively via ipc_server.h

namespace Jugnu
{
    HANDLE IPCServer::hPipe = INVALID_HANDLE_VALUE;
    std::atomic<bool> IPCServer::isRunning{false};
    std::atomic<bool> IPCServer::isClientConnected{false};
    std::mutex IPCServer::pipeMutex;
    HANDLE IPCServer::hConnectEvent = INVALID_HANDLE_VALUE;
    HANDLE IPCServer::hStopEvent = INVALID_HANDLE_VALUE;

    void IPCServer::Start()
    {
        if(isRunning) return;
        isRunning = true;

        // Create synchronization events
        // hConnectEvent: Python signals this to indicate connection/disconnection
        hConnectEvent = CreateEvent(NULL, TRUE, FALSE, NULL);
        
        // hStopEvent: C++ signals this to tell the thread to shut down
        hStopEvent = CreateEvent(NULL, TRUE, FALSE, NULL);
        // NULL → default security
        // TRUE → "manual reset" (we have to manually reset it after it fires)
        // FALSE → starts in "not signaled" state
        // NULL → no name needed


        // Create a background worker thread.
        // If we ran the pipe listener on the main thread, it would block the Win32 Message Loop
        // and our app would completely freeze while waiting for Python to connect.
        CreateThread(NULL, 0, PipeListnerThread, NULL, 0, NULL);
    }

    void IPCServer::Stop()
    {
        isRunning = false;

        // Signal the thread to exit FIRST — unconditionally, regardless of pipe state.
        // Old code gated this inside `if(hPipe != INVALID_HANDLE_VALUE)`, which meant
        // Stop() called before the thread got to CreateNamedPipeA left the thread stuck
        // in WaitForMultipleObjects forever.
        if(hStopEvent != INVALID_HANDLE_VALUE) SetEvent(hStopEvent);

        // Disconnect and close the pipe handle to free OS resources
        if(hPipe != INVALID_HANDLE_VALUE)
        {
            DisconnectNamedPipe(hPipe);
            CloseHandle(hPipe);
            hPipe = INVALID_HANDLE_VALUE;
        }

        // Clean up event handles independently of the pipe
        if(hConnectEvent != INVALID_HANDLE_VALUE)
        {
            CloseHandle(hConnectEvent);
            hConnectEvent = INVALID_HANDLE_VALUE;
        }
        if(hStopEvent != INVALID_HANDLE_VALUE)
        {
            CloseHandle(hStopEvent);
            hStopEvent = INVALID_HANDLE_VALUE;
        }

        std::cout << "\033[1;33m[IPCServer]\033[0m Stopped.\n";
    }

    bool IPCServer::SendMessageToPython(const std::string& message)
    {
        std::lock_guard<std::mutex> lock(pipeMutex);
        if(hPipe == INVALID_HANDLE_VALUE || !isClientConnected) return false;

        DWORD bytesWritten;

        // We append our delimiter so the Python client knows when the JSON payload ends!
        std::string payload = message + "\nEND_OF_MSG\n";

        // WriteFile pushes the bytes straight into the Windows Named Pipe buffer
        BOOL success = WriteFile(
            hPipe,
            payload.c_str(),
            (DWORD)payload.length(),
            &bytesWritten,
            NULL
        );

        if (!success) {
            std::cout << "\033[1;33m[IPCServer]\033[0m Failed to send message to Python. Pipe broken.\n";
            isClientConnected = false;
        } else {
            std::cout << "\033[1;33m[IPCServer]\033[0m \033[32mSuccessfully routed event to Python!\033[0m\n";
        }

        return success == TRUE;
    }

    DWORD WINAPI IPCServer::PipeListnerThread(LPVOID lpParam)
    {
        std::cout<<"\033[1;33m[IPCServer]\033[0m Starting Named Pipe server...\n";

        while(isRunning)
        {
            // CreateNamedPipeA allocates a low-level IPC bridge in the OS kernel.
            // It is significantly faster and more secure than a localhost HTTP server.
            hPipe = CreateNamedPipeA(
                "\\\\.\\pipe\\jugnu_ipc",                       // The exact name Python will look for
                PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED,      // FILE_FLAG_OVERLAPPED -> This single flag tells the OS: "This pipe will use asynchronous I/O. Don't block my thread when I call ReadFile or ConnectNamedPipe on it."
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT, // Block until data arrives
                1,                                             // Max instances (only 1 python script allowed)
                4096,                                           // Output buffer size (4KB)
                4096,                                           // Input buffer size (4KB)
                0,                                              // Default timeout
                NULL                                            // Default security attributes
            );

            if(hPipe == INVALID_HANDLE_VALUE)
            {
                std::cerr<<"\033[1;33m[IPCServer]\033[0m Failed to create pipe. Error: " << GetLastError() << "\n";
                Sleep(1000); // wait a second before trying again
                continue;
            }
            std::cout<<"\033[1;33m[IPCServer]\033[0m Waiting for Python Inference Service to connect...\n";

            OVERLAPPED ol = {0};
            ol.hEvent = hConnectEvent;
            ResetEvent(hConnectEvent);

            ConnectNamedPipe(hPipe, &ol); // Returns immediately in overlapped mode
            DWORD connectErr = GetLastError();

            if (connectErr == ERROR_PIPE_CONNECTED)
            {
                // Fast-connect race: Python was already waiting before we called ConnectNamedPipe.
                // The OS won't auto-signal hConnectEvent in this case, so we skip the wait entirely.
                SetEvent(hConnectEvent);
            }
            else if (connectErr != ERROR_IO_PENDING)
            {
                // Unexpected error — recreate the pipe
                std::cerr << "\033[1;33m[IPCServer]\033[0m ConnectNamedPipe failed. Error: " << connectErr << "\n";
                DisconnectNamedPipe(hPipe);
                CloseHandle(hPipe);
                hPipe = INVALID_HANDLE_VALUE;
                continue;
            }

            // Block until EITHER Python connects (hConnectEvent) OR we are shutting down (hStopEvent)
            // events[0] = hStopEvent  → WAIT_OBJECT_0 + 0 → we are shutting down
            // events[1] = hConnectEvent → WAIT_OBJECT_0 + 1 → Python connected
            HANDLE events[2] = {hStopEvent, hConnectEvent};
            DWORD waitResult = WaitForMultipleObjects(2, events, FALSE, INFINITE);

            if (waitResult == WAIT_OBJECT_0) break; // hStopEvent (index 0) fired → shutdown

            // hConnectEvent (index 1) fired → Python connected!
            isClientConnected = true;
            std::cout << "\033[1;33m[IPCServer]\033[0m \033[1;32mPython connected successfully! IPC bridge active.\033[0m\n";

            // ---- Health-check phase: arm a zero-byte async ReadFile ----
            // The OS will signal hConnectEvent the moment the pipe breaks (Python exits).
            // We don't need to poll at all — zero CPU usage while Python is alive.
            OVERLAPPED olRead = {0};
            olRead.hEvent = hConnectEvent;
            ResetEvent(hConnectEvent);

            DWORD bytesRead = 0;
            ReadFile(hPipe, NULL, 0, &bytesRead, &olRead); // Returns immediately (overlapped)

            // Block until EITHER pipe breaks (hConnectEvent) OR shutdown (hStopEvent)
            // Reuse events[] — swap order so hConnectEvent is index 0 this time
            events[0] = hConnectEvent;
            events[1] = hStopEvent;
            waitResult = WaitForMultipleObjects(2, events, FALSE, INFINITE);

            if (waitResult == WAIT_OBJECT_0 + 1) break; // hStopEvent (index 1) fired → shutdown

            // hConnectEvent fired → pipe broke, Python disconnected
            std::cout << "\033[1;33m[IPCServer]\033[0m Python disconnected. Restarting pipe...\n";
            isClientConnected = false;

            // Cleanup before the outer loop restarts to accept a new connection
            DisconnectNamedPipe(hPipe);
            CloseHandle(hPipe);
            hPipe = INVALID_HANDLE_VALUE;
        }
        return 0;

    }
} // namespace Jugnu