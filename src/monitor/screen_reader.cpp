#include "../monitor/screen_reader.h"
#include "../monitor/win_monitor.h"
#include "../server/ipc_server.h"
#include "../db/db_handler.h"

// WinRT Headers for OCR
#include <winrt/windows.Media.Ocr.h>
#include <winrt/Windows.Graphics.Imaging.h>
#include <winrt/Windows.Globalization.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Storage.Streams.h>
#include <iostream>

using namespace winrt::Windows::Media::Ocr;
using namespace winrt::Windows::Graphics::Imaging;
using namespace winrt::Windows::Globalization;

namespace Jugnu
{
    std::atomic<bool> ScreenReader::isRunning(false);
    HANDLE ScreenReader::hThread = NULL;

    void ScreenReader::Start()
    {
        if(isRunning) return;
        winrt::init_apartment();    // Initialize WinRT
        isRunning = true;
        hThread = CreateThread(NULL, 0, ReaderThread, NULL, 0, NULL);
    }

    void ScreenReader::Stop()
    {
        isRunning = false;
        if(hThread)
        {
            WaitForSingleObject(hThread, 2000); // wait for 2 seconds for the thread to finish
            CloseHandle(hThread);
            hThread = NULL;
        }
    }

    bool ScreenReader::ShouldCapture(const std::string& processName)
    {
        return (processName == "code.exe" || processName == "devenv.exe" || 
                processName == "chrome.exe" || processName == "msedge.exe" || 
                processName == "Acrobat.exe");
    }

    std::wstring ScreenReader::CaptureAndOCR(HWND targetWindow)
    {
        RECT rect;
        if(!GetWindowRect(targetWindow, &rect)) return L"";

        int w = rect.right - rect.left;
        int h = rect.bottom - rect.top;
        if(w <= 0 || h <= 0) return L"";

        // 1. Setup Win32 GDI Device Contexts to capture the screen
        HDC hdcScreen = GetDC(NULL); // Get handle to the entire screen
        HDC hdcMem = CreateCompatibleDC(hdcScreen); // Create an in-memory DC
        HBITMAP hBmp = CreateCompatibleBitmap(hdcScreen, w, h); // Create a bitmap to hold the pixels
        SelectObject(hdcMem, hBmp); // Select the bitmap into the memory DC
        
        // 2. Perform a fast BitBlt (Bit-Block Transfer) from the screen to our memory bitmap
        BitBlt(hdcMem, 0, 0, w, h, hdcScreen, rect.left, rect.top, SRCCOPY);

        // 3. Define the bitmap structure for extracting raw pixels
        // this is used to get the bitmap data from the memory
        BITMAPINFOHEADER bi{};
        bi.biSize = sizeof(BITMAPINFOHEADER);
        bi.biWidth = w;
        bi.biHeight = -h;
        bi.biPlanes = 1;
        bi.biBitCount = 32;
        bi.biCompression = BI_RGB;

        // 4. Extract the raw pixel data from the GDI bitmap into our vector
        std::vector<uint8_t> pixels(w*h*4); // BGRA = 4 bytes per pixel
        GetDIBits(hdcMem, hBmp, 0, h, pixels.data(), (BITMAPINFO*)&bi, DIB_RGB_COLORS);

        // 5. Convert raw Win32 pixels to WinRT SoftwareBitmap
        // WinRT OCR requires a SoftwareBitmap. We bridge the gap using an IBuffer.
        auto buffer = winrt::Windows::Storage::Streams::Buffer(pixels.size());
        memcpy(buffer.data(), pixels.data(), pixels.size());
        buffer.Length(pixels.size()); // Inform the buffer of its actual filled length

        auto bitmap = SoftwareBitmap::CreateCopyFromBuffer(buffer, BitmapPixelFormat::Bgra8, w, h);

        // 6. Initialize the Native Windows 10/11 OCR Engine
        auto engine = OcrEngine::TryCreateFromLanguage(Language(L"en-US"));
        std::wstring resultText = L"";

        if(engine)
        {
            // 7. Execute hardware-accelerated OCR asynchronously, but block with .get()
            auto result = engine.RecognizeAsync(bitmap).get();
            if(result) resultText = std::wstring(result.Text());
        }

        // 8. Clean up GDI handles to prevent massive memory leaks
        DeleteObject(hBmp);
        DeleteDC(hdcMem);
        ReleaseDC(NULL, hdcScreen);

        return resultText;
    }

    DWORD WINAPI ScreenReader::ReaderThread(LPVOID lpParam)
    {
        std::cout << "\033[1;36m[ScreenReader]\033[0m WinRT OCR Engine started.\n";

        while(isRunning)
        {
            // Sleep carefully to allow quick exit (check isRunning frequently)
            // Polling every 30 seconds (30 * 1000ms)
            for(int i=0;i<30&&isRunning;i++) Sleep(1000);
            if(!isRunning) break;

            // Power Optimization: Skip capturing entirely if the user is not actively at the computer
            if(WinMonitor::IsUserIdle()) continue;  
            
            HWND hwnd = GetForegroundWindow();
            if(!hwnd) continue;

            std::string currentApp = WinMonitor::GetProcessName(hwnd);

            // Only capture if the app is explicitly whitelisted (e.g., an IDE or Browser)
            if(ShouldCapture(currentApp))
            {
                std::cout << "\033[90m[ScreenReader]\033[0m Capturing " << currentApp << "...\n";
                
                // Perform the GPU-accelerated capture and OCR
                std::wstring text = CaptureAndOCR(hwnd);

                if(!text.empty())
                {
                    // Convert UTF-16 wide string to UTF-8 standard string for JSON compatibility
                    int size_needed = WideCharToMultiByte(CP_UTF8, 0, &text[0], (int)text.size(), NULL, 0, NULL, NULL);
                    std::string utf8_text(size_needed, 0);
                    WideCharToMultiByte(CP_UTF8, 0, &text[0], (int)text.size(), &utf8_text[0], size_needed, NULL, NULL);

                    // Escape JSON manualy (CRITICAL)
                    std::string escaped_text;
                    for(char c : utf8_text)
                    {
                        switch(c)
                        {
                            case '"': escaped_text += "\\\""; break;
                            case '\\': escaped_text += "\\\\"; break;
                            case '\n': escaped_text += "\\n"; break;
                            case '\r': escaped_text += "\\r"; break;
                            case '\t': escaped_text += "\\t"; break;
                            default: 
                                if (static_cast<unsigned char>(c) < 0x20) {
                                    char buf[7];
                                    snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned char>(c));
                                    escaped_text += buf;
                                } else escaped_text += c;
                                break;
                        }
                    }
                    std::string payload = "{\"type\": \"OCR_SCREEN\", \"app\": \"" + currentApp + "\", \"text\": \"" + escaped_text + "\"}";
                    Jugnu::IPCServer::SendMessageToPython(payload);
                    std::cout << "\033[32m[ScreenReader]\033[0m Sent " << utf8_text.length() << " chars to Python.\n";
                }
            }
        }
        return 0;
    }
}