#pragma once
#include <windows.h>
#include <string>
#include <atomic>

namespace Jugnu
{
    class IPCServer
    {
        public:
            // Start the background pipe listener thread
            static void Start();

            // Stop the listner and safely disconnect the pipe
            static void Stop();

            // Send a string message to the python client
            static bool SendMessageToPython(const std::string& message);

        private:
            // Handle to the Named Pipe allocated by the OS
            static HANDLE hPipe;

            // Atomiv frag to control the while-loop in the listener thred
            static bool isRunning;
            static std::atomic<bool> isClientConnected;

            // The background worker thread that blocks until python connects
            static DWORD WINAPI PipeListnerThread(LPVOID lpParam); // what is LPVOID? -> data type, void pointer (generic pointer)
    };
}