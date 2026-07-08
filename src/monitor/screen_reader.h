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

            // PRIMARY: Reads exact text from the app's accessibility tree (UIA).
            // Zero OCR errors. Works for Chrome, VS Code, Edge, Cursor, Antigravity IDE.
            static std::wstring ExtractTextViaUIA(HWND targetWindow);

            // FALLBACK: Takes a screenshot and runs WinRT OCR on it.
            // Only used if UIA returns empty (e.g., app has no accessibility tree).
            static std::wstring CaptureAndOCR(HWND targetWindow);
            static bool ShouldCapture(const std::string& processName);

            static std::atomic<bool> isRunning;
            static HANDLE hThread;
    };
}