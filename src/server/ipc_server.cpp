#include "ipc_server.h"
#include <iostream>
#include <windows.h>

namespace Jugnu
{
    HANDLE IPCServer::hPipe = INVALID_HANDLE_VALUE;
    bool IPCServer::isRunning = false;
    std::atomic<bool> IPCServer::isClientConnected{false};

    void IPCServer::Start()
    {
        if(isRunning) return;
        isRunning = true;

        // Creater a background worker thread.
        // If we ran the pipe listener on the main thread, it would block the Win32 Message Loop
        // and our app would completely freeze while waiting for Python to connect.
        CreateThread(NULL, 0, PipeListnerThread, NULL, 0, NULL);
    }

    void IPCServer::Stop()
    {
        isRunning = false;
        if(hPipe != INVALID_HANDLE_VALUE)
        {
            // Disconnect and close the handle to free OS resources
            DisconnectNamedPipe(hPipe);
            CloseHandle(hPipe);
            hPipe = INVALID_HANDLE_VALUE;
        }
    }

    bool IPCServer::SendMessageToPython(const std::string& message)
    {
        if(hPipe == INVALID_HANDLE_VALUE || !isClientConnected) return false;

        DWORD bytesWritten;

        // We append our delimiter so the Python client knows when the JSON payload ends!
        std::string payload = message + "\nEND_OF_MSG\n";

        // WriteFile pushes the yted straight into the Windows Named Pipe buffer
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
                PIPE_ACCESS_DUPLEX,                             // Two-way communication (Read/Write)
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

            // ConnectNamedPipe blocks this background thread until inference.py runs
            BOOL connected = ConnectNamedPipe(hPipe, NULL) ? TRUE : (GetLastError() == ERROR_PIPE_CONNECTED);

            if(connected)
            {
                std::cout<<"\033[1;33m[IPCServer]\033[0m \033[1;32mPython connected successfully! IPC bridge active.\033[0m\n";
                isClientConnected = true;

                // Python is a read-only consumer of events. We just monitor pipe health here.
                while(isRunning && isClientConnected)
                {
                    // Check pipe health: if Python disconnects, WriteFile will fail and
                    // set isClientConnected = false, which will break this loop naturally.
                    Sleep(100);
                }

                isClientConnected = false;
                std::cout<<"\033[1;33m[IPCServer]\033[0m Python disconnected. Restarting pipe...\n";
            }

            // Cleanup before the loop restarts to accept a new connection
            DisconnectNamedPipe(hPipe);
            CloseHandle(hPipe);
            hPipe = INVALID_HANDLE_VALUE;
        }
        return 0;
    }
} // namespace Jugnu