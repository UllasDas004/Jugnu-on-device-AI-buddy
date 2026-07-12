#include "monitor/clipboard_manager.h"
#include "server/ipc_server.h"
#include<iostream>

namespace Jugnu
{
    std::atomic<bool> ClipboardManager::isRunning{false};
    HANDLE ClipboardManager::hThread = NULL;
    HWND ClipboardManager::hMessageWindow = NULL;

    void ClipboardManager::Start()
    {
        if(isRunning) return;
        isRunning = true;
        hThread = CreateThread(NULL, 0, ClipboardThread, NULL, 0, NULL);

        std::cout << "\033[1;36m[Clipboard]\033[0m Hooked into OS Clipboard updates.\n";
    }

    void ClipboardManager::Stop()
    {
        isRunning = false;
        if(hThread)
        {
            // WAKE the message loop up so it can exit
            PostThreadMessage(GetThreadId(hThread), WM_QUIT, 0, 0); 
            // Wait for thread to gracefully exit
            WaitForSingleObject(hThread, 1000);
            CloseHandle(hThread);
            hThread = NULL;
        }

        std::cout << "\033[1;36m[Clipboard]\033[0m Stopped.\n";
    }

    DWORD WINAPI ClipboardManager::ClipboardThread(LPVOID lpParam)
    {
        // 1. Register a dummy hidden window class to receive clipboard messages

        WNDCLASS wc = {};
        wc.lpfnWndProc = WindowProc;    // Callback to process messages
        wc.hInstance = GetModuleHandle(NULL);   // Owner instance
        wc.lpszClassName = "JugnuClipboardListenerClass";

        RegisterClass(&wc);

        hMessageWindow = CreateWindowEx(
            0, wc.lpszClassName, "JugnuClipboardListener",
            0, 0, 0, 0, 0, HWND_MESSAGE, NULL, wc.hInstance, NULL
        );

        if(!hMessageWindow) return 1;

        // 2. Hook into the modern Windows Clipboard format listener
        AddClipboardFormatListener(hMessageWindow);
        std::cout << "\033[1;36m[Clipboard]\033[0m Hooked into OS Clipboard updates.\n";

        MSG msg;
        while(GetMessage(&msg, NULL, 0, 0) > 0)
        {
            TranslateMessage(&msg);
            DispatchMessage(&msg);
            if(!isRunning) break;
        }

        RemoveClipboardFormatListener(hMessageWindow);
        DestroyWindow(hMessageWindow);
        return 0;
    }

    LRESULT CALLBACK ClipboardManager::WindowProc(HWND hwnd, UINT uMsg, WPARAM wParam, LPARAM lParam)
    {
        if(uMsg == WM_CLIPBOARDUPDATE)
        {
            ProcessClipboardContent();
            return 0;
        }
        return DefWindowProc(hwnd, uMsg, wParam, lParam);
    }

    // CRITICAL: We must escape newlines, quotes, and backslashes or Python's json.loads() will crash!
    std::string ClipboardManager::EscapeJSON(const std::string& input)
    {
        std::string output;
        output.reserve(input.length() + input.length() / 8);
        for(char c : input)
        {
            switch(c)
            {
                case '"': output += "\\\""; break;
                case '\\': output += "\\\\"; break;
                case '\b': output += "\\b"; break;
                case '\f': output += "\\f"; break;
                case '\n': output += "\\n"; break;
                case '\r': output += "\\r"; break;
                case '\t': output += "\\t"; break;
                default: 
                    if (static_cast<unsigned char>(c) < 0x20) {
                        char buf[7];
                        snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned char>(c));
                        output += buf;
                    } else {
                        output += c;
                    }
                    break;
            }
        }
        return output;
    }

    void ClipboardManager::ProcessClipboardContent()
    {
        if(!OpenClipboard(NULL)) return;

        // Try getting Unicode text (UTF - 16)
        HANDLE hData = GetClipboardData(CF_UNICODETEXT);
        if(hData)
        {
            wchar_t* wText = static_cast<wchar_t*>(GlobalLock(hData));
            if(wText)
            {
                std::wstring wStr(wText);
                GlobalUnlock(hData);

                // Trim massive copies before converting to UTF-8 to prevent slicing multi-byte chars
                if(wStr.length() > 5000)
                {
                    wStr = wStr.substr(0, 5000) + L"...[TRUNCATED]";
                }

                // Convert UTF-16 to UTF-8
                int size_needed = WideCharToMultiByte(CP_UTF8, 0, &wStr[0], (int)wStr.size(), NULL, 0, NULL, NULL);
                std::string utf8_text(size_needed, 0);
                WideCharToMultiByte(CP_UTF8, 0, &wStr[0], (int)wStr.size(), &utf8_text[0], size_needed, NULL, NULL);

                std::cout << "\033[1;36m[Clipboard]\033[0m Intercepted copied text (" << utf8_text.length() << " chars)\n";
                
                std::string payload = "{\"type\": \"CLIPBOARD\", \"text\": \"" + EscapeJSON(utf8_text) + "\"}";
                Jugnu::IPCServer::SendMessageToPython(payload);
            }
        }
        CloseClipboard();
    }
}