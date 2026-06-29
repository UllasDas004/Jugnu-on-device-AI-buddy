#pragma once
#include<windows.h>
#include<string>
#include<atomic>

namespace Jugnu
{
    class ScreenReader
    {
        public:
            static void Start();
            static void Stop();

        private:
            static DWORD WINAPI ReaderThread(LPVOID lpParam);
            static std::wstring CaptureAndOCR(HWND targetWindow);
            static bool ShouldCapture(const std::string& processName);

            static std::atomic<bool> isRunning;
            static HANDLE hThread;
    };
}