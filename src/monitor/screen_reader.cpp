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

        std::cout << "\033[1;32m[ScreenReader]\033[0m Started.\n";
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

        std::cout << "\033[1;32m[ScreenReader]\033[0m Stopped.\n";
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

    struct UiaSection
    {
        std::wstring typeName;      // "Edit", "Document", "Text"
        std::wstring automationId;  // smantic role hint from the UIA tree
        std::wstring text;          // verbatim extracted content
    };

    // Whitelist: only content-bearing leaf controls.
    // Lists, trees, menus, toolbars, status bars are NOT in this set → auto-skipped.
    static const std::unordered_set<CONTROLTYPEID> kContentTypes = {
        UIA_EditControlTypeId,      // code editors (Monaco in Chrome/VS Code/Clion)
        UIA_DocumentControlTypeId,  // rich text: problem descriptions, docs, READMEs
        UIA_TextControlTypeId,      // plain text paragraphs
    };

    // Helper: Convert UIA CONTROLTYPEID to a readable string
    static std::wstring ControlTypeName(CONTROLTYPEID id)
    {
        switch(id)
        {
            case UIA_EditControlTypeId:     return L"Edit";
            case UIA_DocumentControlTypeId: return L"Document";
            case UIA_TextControlTypeId:     return L"Text";
            default:                        return L"Unknown";
        }
    }

    // Helper: JSON escape for C++ wide strings (needed for "text": "...")
    // Without this, "Hello\nWorld" breaks the JSON structure.
    static std::wstring JsonEscapeW(const std::wstring& s)
    {
        std::wstring out;
        out.reserve(s.size() + 16);
        for(wchar_t c : s)
        {
            switch (c) {
            case L'"':  out += L"\\\""; break;
            case L'\\': out += L"\\\\"; break;
            case L'\n': out += L"\\n";  break;
            case L'\r': out += L"\\r";  break;
            case L'\t': out += L"\\t";  break;
            default:
                if (c < 0x20) break;  // drop other control chars
                out += c;
            }
        }
        return out;
    }

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

        std::vector<UiaSection> candidates;

        if(pElemnts)
        {
            int count = 0;
            pElemnts->get_Length(&count);
            // 5. BFS through every element in the UIA tree.
            for(int i=0;i<count;i++)
            {
                IUIAutomationElement* pEl = nullptr;
                if(FAILED(pElemnts->GetElement(i, &pEl)) || !pEl) continue;

                // ── Only process whitelisted content types ────────────────────────
                CONTROLTYPEID ctrlType = 0;
                pEl->get_CurrentControlType(&ctrlType);
                if(!kContentTypes.count(ctrlType))
                {
                    pEl->Release();
                    continue;  // skip List, Tree, Menu, StatusBar, ToolBar, etc.
                }

                // ── Extract text via TextPattern ──────────────────────────────────
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
                            size_t min_required = (ctrlType == UIA_EditControlTypeId) ? 20 : MIN_TEXT;
                            if(text.length() >= min_required && text.length() <= MAX_TEXT)
                            {
                                // ── Get automation ID for Python-side context ─────
                                std::wstring autoId;
                                BSTR bId = nullptr;
                                if(SUCCEEDED(pEl->get_CurrentAutomationId(&bId)) && bId)
                                {
                                    autoId = std::wstring(bId, SysStringLen(bId));
                                    SysFreeString(bId);
                                }

                                UiaSection sec{
                                    ControlTypeName(ctrlType),
                                    autoId,
                                    text
                                };

                                // Dedup: prevent parent nodes from duplicating children
                                bool absorbed = false;
                                for(auto& existing : candidates)
                                {
                                    // Never let Document/Text absorb an Edit, and vice versa.
                                    // Edits (code blocks) must be preserved independently.
                                    if(existing.typeName == L"Edit" && sec.typeName != L"Edit") continue;
                                    if(sec.typeName == L"Edit" && existing.typeName != L"Edit") continue;

                                    if(text.find(existing.text) != std::wstring::npos)
                                    {
                                        existing = sec;    // new is a superset — promote it
                                        absorbed = true;
                                        break;
                                    }
                                    if(existing.text.find(text) != std::wstring::npos)
                                    {
                                        absorbed = true; // already captured by a larger block
                                        break;
                                    }
                                }
                                if(!absorbed) candidates.push_back(sec);
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
        //── COM cleanup (fix original leak: pCond/pRoot/pAutomation never freed) ─
        pTrueCondition->Release();
        pRoot->Release();
        pAutomation->Release();

        if(candidates.empty()) return L"";

        // Sort by length descending — richest content first
        std::sort(candidates.begin(), candidates.end(),
            [](const UiaSection& a, const UiaSection& b){ return a.text.length() > b.text.length(); });

        // Cap at 5 sections — Problem Statement, Code, and Notes
        if(candidates.size() > 5) candidates.resize(5);

        // ── Serialize to JSON array → Python reads with json.loads() ─────────────
        std::wstring json = L"[";
        for (size_t i = 0; i < candidates.size(); i++)
        {
            if (i > 0) json += L",";
            json += L"{\"type\":\"" + candidates[i].typeName;
            json += L"\",\"name\":\"" + JsonEscapeW(candidates[i].automationId);
            json += L"\",\"text\":\"" + JsonEscapeW(candidates[i].text) + L"\"}";
        }
        json += L"]";
        return json;
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
        winrt::init_apartment(); 
        g_ocrEngine = OcrEngine::TryCreateFromLanguage(Language(L"en-US"));
        std::cout << "\033[1;36m[ScreenReader]\033[0m UIA + OCR Engine started.\n";

        bool hasOcredWhileIdle = false;
        bool wasHibernating = true;
        std::string lastOcredApp = "";

        while(isRunning)
        {
            // PHASE 1: Hibernate. Sleep infinitely while user is gaming/watching a movie.
            // WinMonitor::WinEventProc calls SetEvent(hDeepWorkEvent) when a work app opens.
            if(wasHibernating)
                std::cout << "\033[90m[ScreenReader]\033[0m Standby - waiting for a focus session to begin.\n";
            WaitForSingleObject(Jugnu::hDeepWorkEvent, INFINITE);
            if(!isRunning) break;

            if(wasHibernating)
            {
                std::cout << "\033[1;32m[ScreenReader]\033[0m Focus session detected - screen capture guard is live.\n";
                wasHibernating = false;
            }

            // PHASE 2: User is in a Deep Work app. Use dynamic math.
            LASTINPUTINFO lii;
            lii.cbSize = sizeof(LASTINPUTINFO);
            if(!GetLastInputInfo(&lii))
            {
                Sleep(2000);
                continue;
            }
            
            DWORD idleTime = GetTickCount() - lii.dwTime;

            if(idleTime >= 60000)   // 60 seconds idle → capture screen
            {
                HWND hwnd = GetForegroundWindow();
                if(!hwnd)
                {
                    Sleep(2000);
                    continue;
                }
                std:: string currentApp = WinMonitor::GetProcessName(hwnd);
                // [MID-IDLE APP SWITCHING LOGIC]
                // If a user sits idle in VS Code for 60s, we capture it and set hasOcredWhileIdle = true.
                // If they then Alt-Tab to Chrome without touching the mouse, system idleTime stays > 60s.
                // By checking if currentApp changed, we instantly drop the lock to capture the new window.
                if(currentApp != lastOcredApp) hasOcredWhileIdle = false;
                
                if(!hasOcredWhileIdle)
                {
                    hasOcredWhileIdle = true;
                    
                    // Mark this app as captured so we don't spam it in a loop
                    lastOcredApp = currentApp;

                    // Only capture if the app supports UIA extraction
                    if(ShouldCapture(currentApp))
                    {
                        std::cout << "\033[1;36m[ScreenReader]\033[0m 60s idle detected in '" << currentApp << "' — reading screen context...\n";

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
                            std::cout << "\033[33m[ScreenReader]\033[0m UIA returned nothing - falling back to OCR for "
                                    << currentApp << "...\n";
                            text = CaptureAndOCR(hwnd);
                        }

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
                                std::cout << "\033[32m[ScreenReader]\033[0m Queued " 
                                        << utf8_text.length() << " chars from " 
                                        << currentApp << " pending synthesis by Gemma.\n";
                            }
                        }
                        else
                        {
                            std::cout << "\033[33m[ScreenReader]\033[0m Both UIA and OCR returned empty for " << currentApp << ". Nothing buffered.\n";
                        }
                    }
                    else
                    {
                        std::cout << "\033[90m[ScreenReader]\033[0m '" << currentApp << "' is not in the capture list \u2014 skipping context read.\n";
                    }
                }
                Sleep(2000);    // Stay in slow loop while they remain idle
            }
            else
            {
                // User is active. Check if we left the focus zone.
                DWORD eventState = WaitForSingleObject(Jugnu::hDeepWorkEvent, 0);
                if(eventState == WAIT_TIMEOUT)
                {
                    std::cout << "\033[90m[ScreenReader]\033[0m Left the focus zone screen capture entering standby.\n";
                    wasHibernating = true;
                    hasOcredWhileIdle = false;
                    lastOcredApp = "";
                    continue;
                }

                hasOcredWhileIdle = false;
                lastOcredApp = "";
                DWORD timeRemaining = 60000 - idleTime;
                std::cout << "\033[90m[ScreenReader]\033[0m User is active. Screen guard sleeping for "
                          << (timeRemaining / 1000) << "s.\n";
                Sleep(timeRemaining);
            }
        }
        CoUninitialize();   // Cleanup COM on thread exit
        return 0;
    }
}