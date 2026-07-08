#include "../monitor/screen_reader.h"
#include "../monitor/win_monitor.h"
#include "../server/ipc_server.h"
#include "../db/db_handler.h"

// ── UIA (UI Automation) Headers ──────────────────────────────────────────────
// UIAutomation.h is the single master header — it pulls in all UIA COM interfaces:
// IUIAutomation, IUIAutomationElement, IUIAutomationTextPattern, etc.
#include <UIAutomation.h>

// ── WinRT OCR Headers (kept as fallback) ─────────────────────────────────────
#include <winrt/windows.Media.Ocr.h>
#include <winrt/Windows.Graphics.Imaging.h>
#include <winrt/Windows.Globalization.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Storage.Streams.h>
#include <iostream>
#include <unordered_set>
#include <vector>
#include <algorithm>

using namespace winrt::Windows::Media::Ocr;
using namespace winrt::Windows::Graphics::Imaging;
using namespace winrt::Windows::Globalization;

namespace Jugnu
{
    std::atomic<bool> ScreenReader::isRunning(false);
    HANDLE ScreenReader::hThread = NULL;
    static winrt::Windows::Media::Ocr::OcrEngine g_ocrEngine = nullptr;

    void ScreenReader::Start()
    {
        if(isRunning) return;
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
        // P1-FIX: Use an unordered_set for O(1) lookup and easy extension.
        // Previously a linear chain of == comparisons — silent failure for any
        // IDE not in this exact list (Cursor, Zed, IntelliJ, etc.)
        static const std::unordered_set<std::string> CAPTURE_APPS = {
            "code.exe",    // VS Code
            "devenv.exe",  // Visual Studio
            "chrome.exe",  // Chrome
            "msedge.exe",  // Edge
            "Acrobat.exe", // PDF reader
            "cursor.exe",  // Cursor IDE
            "idea64.exe",  // IntelliJ IDEA
            "clion64.exe", // CLion
            "fleet.exe",   // Fleet
            "Antigravity IDE.exe",  // Antigravity IDE (Electron/Chromium)
        };
        return CAPTURE_APPS.count(processName) > 0;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // PRIMARY EXTRACTION: Windows UI Automation
    //
    // How this works:
    // 1. We create an IUIAutomation COM object — this is Windows' accessibility engine.
    // 2. We get the UIA element for the target window (the Chrome/VS Code process).
    // 3. We do a breadth-first search (BFS) through all child elements in the UIA tree.
    // 4. For each element, we query its IUIAutomationTextPattern.
    //    TextPattern is the UIA interface for reading raw text from any control.
    // 5. We pick the element with the LONGEST text — this is almost always the code editor.
    // 6. GetText(-1, ...) returns the complete text with correct newlines + indentation.
    //    The -1 argument means "no character limit — give me everything".
    //
    // Why BFS instead of searching for a specific control type?
    //   Monaco (LeetCode, VS Code) can register as Document, Edit, or Group depending
    //   on the Chrome version. Searching for the longest text is universal and robust.
    // ─────────────────────────────────────────────────────────────────────────
    std::wstring ScreenReader::ExtractTextViaUIA(HWND targetWindow)
    {
        // 1. Initialize the UIA COM factory.
        //    CoCreateInstance is the standard way to create a COM object.
        //    CLSID_CUIAutomation8 is the GUID for the UIA factory class.
        //    IID_IUIAutomation is the interface we want back.
        IUIAutomation* pAutomation = nullptr;
        HRESULT hr = CoCreateInstance(
            CLSID_CUIAutomation8,
            nullptr,
            CLSCTX_INPROC_SERVER,
            IID_IUIAutomation,
            (void**)&pAutomation
        );
        if(FAILED(hr) || !pAutomation) return L"";

        // 2. Get the root UIA element for the target HWND (our Chrome/VS Code window).
        //    ElementFromHandle() maps a Win32 HWND → IUIAutomationElement.
        IUIAutomationElement* pRoot = nullptr;
        hr = pAutomation->ElementFromHandle(targetWindow, &pRoot);
        if(FAILED(hr) || !pRoot)
        {
            pAutomation->Release();
            return L"";
        }

        // 3. Build a condition that matches ALL elements (TrueCondition = no filter).
        //    We want to walk every node in the subtree to find the richest text.
        IUIAutomationCondition* pTrueCondition = nullptr;
        hr = pAutomation->CreateTrueCondition(&pTrueCondition); // Bug1 fix: capture return value
        if(FAILED(hr) || !pTrueCondition)
        {
            pRoot->Release();
            pAutomation->Release();
            return L"";
        }

        // 4. Get a flat array of ALL descendant elements under the root.
        //    TreeScope_Descendants = walk the entire subtree recursively.
        IUIAutomationElementArray* pElemnts = nullptr;
        hr = pRoot->FindAll(TreeScope_Descendants, pTrueCondition, &pElemnts); // Bug2 fix: capture return value

        // 5. Collect multiple independent text sections instead of just the longest
        const size_t MIN_TEXT = 100;    // skip tiny buttons/labels
        const size_t MAX_TEXT = 150000; // kip the root "all-page" element that subsumes everything

        std::vector<std::wstring> candidates;

        if(pElemnts)
        {
            int count = 0;
            pElemnts->get_Length(&count);
            // 5. BFS through every element in the UIA tree.
            for(int i=0;i<count;i++)
            {
                IUIAutomationElement* pEl = nullptr;
                if(FAILED(pElemnts->GetElement(i, &pEl)) || !pEl) continue;

                // 6. Query the TextPattern interface from this element.
                //    Not every element has a TextPattern — most buttons and labels don't.
                //    GetCurrentPattern() returns E_NOINTERFACE if the pattern isn't supported.

                IUIAutomationTextPattern* pTextPattern = nullptr;
                hr = pEl->GetCurrentPattern(UIA_TextPatternId, (IUnknown**)&pTextPattern);

                if(SUCCEEDED(hr) && pTextPattern)
                {
                    // 7. Get the full document text range and extract the raw string.
                    IUIAutomationTextRange* pRange = nullptr;
                    if(SUCCEEDED(pTextPattern->get_DocumentRange(&pRange)) && pRange)
                    {
                        BSTR bstr = nullptr;
                        // -1 = no character limit, retrieve everything
                        if(SUCCEEDED(pRange->GetText(-1, &bstr)) && bstr)
                        {
                            std::wstring text(bstr, SysStringLen(bstr));
                            SysFreeString(bstr);
                            // 8. Keep only the longest text we've found.
                            //    The code editor always has the most characters.
                            if(text.length() >= MIN_TEXT && text.length() <= MAX_TEXT)
                            {
                                // Dedup: prevent parent nodes from duplicating children
                                bool absorbed = false;
                                for(auto& existing : candidates)
                                {
                                    if(text.find(existing) != std::wstring::npos)
                                    {
                                        existing = text;    // new is a superset — promote it
                                        absorbed = true;
                                        break;
                                    }
                                    if(existing.find(text) != std::wstring::npos)
                                    {
                                        absorbed = true; // already captured by a larger block
                                        break;
                                    }
                                }
                                if(!absorbed) candidates.push_back(text);
                            }
                        }
                        pRange->Release();
                    }
                    pTextPattern->Release();
                }
                pEl->Release();
            }
            pElemnts->Release();
        }
        // Sort by length descending — richest content first
        std::sort(candidates.begin(), candidates.end(),
            [](const std::wstring& a, const std::wstring& b){ return a.length() > b.length(); });

        // Cap at 3 sections — Problem Statement, Code, and Notes
        if(candidates.size() > 3) candidates.resize(3);

        // 9. Cleanup all COM references. COM uses reference counting —
        //    every object you get must be Release()'d or you leak memory.

        // Join with a clear separator so Python can see section boundaries
        std::wstring bestText = L"";
        for(size_t i = 0; i < candidates.size(); i++)
        {
            bestText += L"===SECTION===\n";
            bestText += candidates[i];
            bestText += L"\n";
        }
        return bestText;
    }


    // ─────────────────────────────────────────────────────────────────────────
    // FALLBACK EXTRACTION: GDI Screenshot + WinRT OCR
    // Only called when UIA returns empty string.
    // ─────────────────────────────────────────────────────────────────────────
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
        std::wstring resultText = L"";

        if(g_ocrEngine)
        {
            // 7. Execute hardware-accelerated OCR asynchronously, but block with .get()
            auto result = g_ocrEngine.RecognizeAsync(bitmap).get();
            if(result) resultText = std::wstring(result.Text());
        }

        // 8. Clean up GDI handles to prevent massive memory leaks
        DeleteObject(hBmp);
        DeleteDC(hdcMem);
        ReleaseDC(NULL, hdcScreen);

        return resultText;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // READER THREAD — Main loop
    // ─────────────────────────────────────────────────────────────────────────
    DWORD WINAPI ScreenReader::ReaderThread(LPVOID lpParam)
    {
        // Bug4 fix: Do NOT call CoInitializeEx manually before winrt::init_apartment().
        // winrt::init_apartment() calls CoInitializeEx internally as STA (apartment-threaded).
        // Calling CoInitializeEx first with COINIT_MULTITHREADED and then winrt with STA
        // returns RPC_E_CHANGED_MODE — a silent apartment model conflict.
        // Let WinRT own COM initialization for this thread entirely.
        winrt::init_apartment(); // Initializes COM as STA — correct for both WinRT OCR and UIA
        g_ocrEngine = OcrEngine::TryCreateFromLanguage(Language(L"en-US"));
        std::cout << "\033[1;36m[ScreenReader]\033[0m UIA + OCR Engine started.\n";


        bool hasOcredWhileIdle = false;
        while(isRunning)
        {
            // Poll faster (every 2 seconds) so we can catch the exact moment they go idle
            for(int i=0;i<2&&isRunning;i++) Sleep(1000);
            if(!isRunning) break;

            // Check if user has been idle for the threshold (e.g., 30s)
            bool isIdle = WinMonitor::IsUserIdle();
            if(!isIdle)
            {
                // User is actively moving mouse/typing. Reset flag, but DON'T OCR yet.
                hasOcredWhileIdle = false;
                continue;
            }

            if(isIdle && hasOcredWhileIdle) continue;

            hasOcredWhileIdle = true;
            
            HWND hwnd = GetForegroundWindow();
            if(!hwnd) continue;

            std::string currentApp = WinMonitor::GetProcessName(hwnd);

            // Only capture if the app is explicitly whitelisted (e.g., an IDE or Browser)
            if(ShouldCapture(currentApp))
            {
                // ── PRIMARY: Try UIA first ────────────────────────────────
                std::cout << "\033[90m[ScreenReader]\033[0m Trying UIA for " << currentApp << "...\n";
                std::wstring text = ExtractTextViaUIA(hwnd);
                if(!text.empty())
                {
                    std::cout << "\033[32m[ScreenReader]\033[0m UIA extracted "
                              << text.length() << " chars from " << currentApp << ".\n";
                }
                else
                {
                    // ── FALLBACK: OCR if UIA returned nothing ─────────────
                    std::cout << "\033[33m[ScreenReader]\033[0m UIA empty — falling back to OCR for "
                              << currentApp << "...\n";
                    text = CaptureAndOCR(hwnd);
                }

                // Bug3 fix: removed stale "Capturing" log line leftover from OCR-only version

                if(!text.empty())
                {
                    // Convert UTF-16 wide string to UTF-8 standard string for JSON compatibility
                    int size_needed = WideCharToMultiByte(CP_UTF8, 0, &text[0], (int)text.size(), NULL, 0, NULL, NULL);
                    std::string utf8_text(size_needed, 0);
                    WideCharToMultiByte(CP_UTF8, 0, &text[0], (int)text.size(), &utf8_text[0], size_needed, NULL, NULL);

                    // ARCHITECTURE CHANGE: Write directly to SQLite ocr_buffer.
                    // No JSON escaping needed — SQLite prepared statements handle all special chars safely.
                    // No IPC pipe involved — large OCR text blobs never travel over Named Pipes.
                    // Python's FlushWorker will read this table every 60s and clean it with Gemma.

                    if(DBHandler::BufferOCR(currentApp, utf8_text))
                    {
                        std::cout << "\033[32m[ScreenReader]\033[0m Buffered " 
                                  << utf8_text.length() << " chars from " 
                                  << currentApp << " to ocr_buffer.\n";
                    }
                }
            }
        }
        CoUninitialize();   // Cleanup COM on thread exit
        return 0;
    }
}