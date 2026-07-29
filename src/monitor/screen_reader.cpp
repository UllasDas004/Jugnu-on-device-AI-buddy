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
#include <stack>

// Register the Ignore tag globally so Windows knows not to track programmatic restorations
static UINT cfIgnore = RegisterClipboardFormatA("ExcludeClipboardContentFromMonitorProcessing");

using namespace winrt::Windows::Media::Ocr;
using namespace winrt::Windows::Graphics::Imaging;
using namespace winrt::Windows::Globalization;

namespace Jugnu
{
    std::atomic<ULONGLONG> g_ghostClipboardIgnoreUntilTick{0};
    std::atomic<DWORD> g_lastGhostClipboardInputTime{0};
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
        std::wstring automationId;  // semantic role hint from the UIA tree
        std::wstring text;          // verbatim extracted content
        bool fullBuffer = false;
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

    // ── Ghost Clipboard: Full Code Buffer Extraction ──────────────────────────
    // Calls SetFocus() on the Monaco editor element via UIA accessibility API,
    // then synthesizes Ctrl+A + Ctrl+C to read Monaco's full JS model from clipboard.
    // The user's original clipboard is backed up and restored atomically.
    // Only called during 60s idle — user is not typing, safe to synthesize input.
    // Returns empty wstring if SetFocus fails and bounding-rect fallback also fails.
    // ─────────────────────────────────────────────────────────────────────────────
    static std::wstring GhostClipboard(IUIAutomationElement* pMonacoEl)
    {
        // Step 1: Force focus onto Monaco via UIA.
        // Chrome bridges SetFocus() → DOM focus() on the renderer element.
        HRESULT hr = pMonacoEl->SetFocus();
        if(FAILED(hr))
        {
            // Fallback: synthetic click at the center of the editor's bounding rect.
            RECT rect{};
            if(SUCCEEDED(pMonacoEl->get_CurrentBoundingRectangle(&rect)))
            {
                int cx = rect.left + (rect.right - rect.left) / 2;
                int cy = rect.top + (rect.bottom - rect.top) / 2;
                INPUT inputs[2] = {};
                inputs[0].type  = INPUT_MOUSE;
                inputs[0].mi.dx = cx * 65535 / GetSystemMetrics(SM_CXSCREEN);
                inputs[0].mi.dy = cy * 65535 / GetSystemMetrics(SM_CYSCREEN);
                inputs[0].mi.dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTDOWN;
                inputs[1] = inputs[0];
                inputs[1].mi.dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTUP;
                SendInput(2, inputs, sizeof(INPUT));
            }
        }
        Sleep(60); // Wait for Chrome renderer IPC to deliver focus (~10-30ms latency)

        // Step 2 : Backup the user's current clipboard content.
        std::wstring backup;
        HWND hOwner = GetConsoleWindow();
        if(OpenClipboard(hOwner))
        {
            HANDLE hData = GetClipboardData(CF_UNICODETEXT);
            if(hData)
            {
                wchar_t* p = static_cast<wchar_t*>(GlobalLock(hData));
                if(p)
                {
                    backup = p;
                    GlobalUnlock(hData);
                }
            }
            CloseClipboard();
        }

        // Step 3: Set ignore flag far into the future during extraction
        g_ghostClipboardIgnoreUntilTick = GetTickCount64() + 10000;

        // Step 4: Ctrl+A — Select All. Monaco selects its full JS model, not just viewport.
        INPUT selAll[4] = {};
        selAll[0].type = INPUT_KEYBOARD;
        selAll[0].ki.wVk = VK_CONTROL;
        selAll[1].type = INPUT_KEYBOARD;
        selAll[1].ki.wVk = 'A';
        selAll[2] = selAll[1];
        selAll[2].ki.dwFlags = KEYEVENTF_KEYUP;
        selAll[3] = selAll[0];
        selAll[3].ki.dwFlags = KEYEVENTF_KEYUP;
        SendInput(4, selAll, sizeof(INPUT));
        Sleep(30);

        // Step 5: Ctrl+C — Copy. Monaco serializes the full buffer into clipboard.
        INPUT copy[4] = {};
        copy[0].type = INPUT_KEYBOARD;
        copy[0].ki.wVk = VK_CONTROL;
        copy[1].type = INPUT_KEYBOARD;
        copy[1].ki.wVk = 'C';
        copy[2] = copy[1];
        copy[2].ki.dwFlags = KEYEVENTF_KEYUP;
        copy[3] = copy[0];
        copy[3].ki.dwFlags = KEYEVENTF_KEYUP;
        SendInput(4, copy, sizeof(INPUT));
        Sleep(150); // Monaco clipboard write is async — give it time

        // Step 6: Read the full code buffer.
        std::wstring codeBuffer;
        if(OpenClipboard(hOwner))
        {
            HANDLE hData = GetClipboardData(CF_UNICODETEXT);
            if(hData)
            {
                wchar_t* p = static_cast<wchar_t*>(GlobalLock(hData));
                if(p)
                {
                    codeBuffer = p;
                    GlobalUnlock(hData);
                }
            }
            CloseClipboard();
        }

        // Step 7: Restore the user's original clipboard content.
        if(OpenClipboard(hOwner))
        {
            EmptyClipboard();
            if(!backup.empty())
            {
                HGLOBAL hMem = GlobalAlloc(GMEM_MOVEABLE, (backup.size() + 1) * sizeof(wchar_t));
                if(hMem)
                {
                    wchar_t* p = static_cast<wchar_t*>(GlobalLock(hMem));
                    if(p)
                    {
                        wmemcpy(p, backup.c_str(), (backup.size() + 1));
                        GlobalUnlock(hMem);
                        SetClipboardData(CF_UNICODETEXT, hMem);
                        // Note: do NOT call GlobalFree — SetClipboardData takes ownership

                        // Inject the Ignore Tag alongside the restored text.
                        // This prevents Win+V and other clipboard managers from capturing this restoration event.
                        SetClipboardData(cfIgnore, NULL); 
                    }
                }
            }
            CloseClipboard();
        }

        // Step 8: Lower flag — allow 1000ms for async WM_CLIPBOARDUPDATE to pass
        g_ghostClipboardIgnoreUntilTick = GetTickCount64() + 1000;

        // Step 9: Clear the visual "Select All" highlight in the editor by pressing Right Arrow
        INPUT rightArrow[2] = {};
        rightArrow[0].type = INPUT_KEYBOARD;
        rightArrow[0].ki.wVk = VK_RIGHT;

        rightArrow[1].type = INPUT_KEYBOARD;
        rightArrow[1].ki.wVk = VK_RIGHT;
        rightArrow[1].ki.dwFlags = KEYEVENTF_KEYUP;
        
        SendInput(2, rightArrow, sizeof(INPUT));
        Jugnu::g_lastGhostClipboardInputTime = GetTickCount();

        return codeBuffer; // empty string if anything failed
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

        // 3. Prepare the Tree walker for manual BFS
        IUIAutomationTreeWalker* pWalker = nullptr;
        pAutomation->get_ControlViewWalker(&pWalker);
        if(!pWalker)
        {
            pRoot->Release();
            pAutomation->Release();
            return L"";
        }

        std::vector<UiaSection> candidates;
        IUIAutomationElement* pMonacoEl = nullptr;  // saved for Ghost Clipboard
        IUIAutomationElement* pRootWebArea = nullptr;  // saved for URL + correct page title
        size_t largestMonacoLen = 0;

        // ── 1. SETUP DFS STACK ──
        std::stack<IUIAutomationElement*> dfsStack;
        dfsStack.push(pRoot);
        pRoot->AddRef(); 
        bool foundMainLandmark = false;
        while(!dfsStack.empty())
        {
            IUIAutomationElement* pNode = dfsStack.top();
            dfsStack.pop();

            // ── 2. EARLY PRUNING: Visibility ──
            BOOL isOffscreen = FALSE;
            pNode->get_CurrentIsOffscreen(&isOffscreen);
            if(isOffscreen)
            {
                pNode->Release();
                continue;
            }

            CONTROLTYPEID type = 0;
            pNode->get_CurrentControlType(&type);

            // ── 3. ARIA LANDMARK PRUNING (The Holy Grail) ──
            bool isMainContent = false;
            BSTR bLocalType = nullptr;
            if (SUCCEEDED(pNode->get_CurrentLocalizedControlType(&bLocalType)) && bLocalType)
            {
                std::wstring localType(bLocalType, SysStringLen(bLocalType));
                SysFreeString(bLocalType);
                
                // Prune web layout sidebars and footers entirely (which contain the ads/comments)
                // We keep 'navigation' and 'banner' so we can still extract the browser URL bar!
                if (localType == L"complementary" || localType == L"contentinfo")
                {
                    pNode->Release();
                    continue; // Skip extracting this AND skip all its children!
                }
                if (localType == L"main") isMainContent = true;
            }

            // ── SPECIAL: Save RootWebArea for URL + page title extraction ─────────────────
            // Chrome exposes the active tab's URL via LegacyIAccessible on the RootWebArea.
            // This is reliable at idle (unlike the omnibox which is empty unless focused).
            if(type == UIA_DocumentControlTypeId && !pRootWebArea)
            {
                BSTR bName = nullptr;
                if(SUCCEEDED(pNode->get_CurrentName(&bName)) && bName)
                {
                    std::wstring name(bName, SysStringLen(bName));
                    SysFreeString(bName);
                    // RootWebArea's Name is the page title — Chrome always sets this
                    if(!name.empty())
                    {
                        pRootWebArea = pNode;
                        pRootWebArea->AddRef();
                    }
                }
            }

            // Prune dead-end UI branches
            if(type == UIA_ToolBarControlTypeId || type == UIA_MenuBarControlTypeId ||
               type == UIA_ScrollBarControlTypeId || type == UIA_StatusBarControlTypeId ||
               type == UIA_TitleBarControlTypeId || type == UIA_TabItemControlTypeId)
            {
                pNode->Release();
                continue;
            }

            // ── 4. EXTRACT TEXT ──
            // If it's a content type OR it's the ARIA "main" container
            if(kContentTypes.count(type) || isMainContent)
            {
                std::wstring text = L"";
                
                IUIAutomationTextPattern* pTextPattern = nullptr;
                if(SUCCEEDED(pNode->GetCurrentPattern(UIA_TextPatternId, (IUnknown**)&pTextPattern)) && pTextPattern)
                {
                    IUIAutomationTextRange* pRange = nullptr;
                    if(SUCCEEDED(pTextPattern->get_DocumentRange(&pRange)) && pRange)
                    {
                        BSTR bstr = nullptr;
                        if(SUCCEEDED(pRange->GetText(-1, &bstr)) && bstr)
                        {
                            text = std::wstring(bstr, SysStringLen(bstr));
                            SysFreeString(bstr);
                        }
                        pRange->Release();
                    }
                    pTextPattern->Release();
                }

                if(text.empty())
                {
                    IUIAutomationValuePattern* pValuePattern = nullptr;
                    if(SUCCEEDED(pNode->GetCurrentPattern(UIA_ValuePatternId, (IUnknown**)&pValuePattern)) && pValuePattern)
                    {
                        BSTR bstr = nullptr;
                        if(SUCCEEDED(pValuePattern->get_CurrentValue(&bstr)) && bstr)
                        {
                            text = std::wstring(bstr, SysStringLen(bstr));
                            SysFreeString(bstr);
                        }
                        pValuePattern->Release();
                    }
                }

                size_t min_required = (type == UIA_EditControlTypeId) ? 20 : 100;
                if(text.length() >= min_required && text.length() <= 150000)
                {
                                std::wstring autoId;
                                BSTR bId = nullptr;
                                if(SUCCEEDED(pNode->get_CurrentAutomationId(&bId)) && bId)
                                {
                                    autoId = std::wstring(bId, SysStringLen(bId));
                                    SysFreeString(bId);
                                }
                                std::wstring typeNameForJson = isMainContent ? L"MainContent" : ControlTypeName(type);
                                UiaSection sec{ typeNameForJson, autoId, text };

                                // Dedup logic
                                bool absorbed = false;
                                for(auto& existing : candidates)
                                {
                                    // Protect Edit controls from being absorbed or absorbing others
                                    if(existing.typeName == L"Edit" && sec.typeName != L"Edit") continue;
                                    if(sec.typeName == L"Edit" && existing.typeName != L"Edit") continue;
                                    
                                    // Protect MainContent from being absorbed by Document
                                    if(existing.typeName == L"MainContent" && sec.typeName != L"MainContent") continue;
                                    if(sec.typeName == L"MainContent" && existing.typeName != L"MainContent") continue;

                                    if(text.find(existing.text) != std::wstring::npos) {
                                        existing = sec; absorbed = true; break;
                                    }
                                    if(existing.text.find(text) != std::wstring::npos) {
                                        absorbed = true; break;
                                    }
                                }
                                if(!absorbed) candidates.push_back(sec);
                                
                                if (isMainContent) foundMainLandmark = true;
                                // Save Edit for Ghost Clipboard
                                if(type == UIA_EditControlTypeId && text.length() > largestMonacoLen)
                                {
                                    if(pMonacoEl) pMonacoEl->Release();
                                    pMonacoEl = pNode;
                                    pMonacoEl->AddRef();
                                    largestMonacoLen = text.length();
                                }
                            }

            }
            // ── 5. ENQUEUE CHILDREN (DFS Reverse Order) ──
            IUIAutomationElement* pChild = nullptr;
            if(SUCCEEDED(pWalker->GetFirstChildElement(pNode, &pChild)) && pChild)
            {
                std::vector<IUIAutomationElement*> children;
                while(pChild)
                {
                    children.push_back(pChild);
                    IUIAutomationElement* pNext = nullptr;
                    pWalker->GetNextSiblingElement(pChild, &pNext);
                    pChild = pNext;
                }
                // Push in reverse order so the topmost visual child is popped FIRST
                for (auto it = children.rbegin(); it != children.rend(); ++it)
                dfsStack.push(*it);
            }
            pNode->Release();
        }
        
        pWalker->Release();

        // ── 6a. EXTRACT URL + PAGE TITLE from RootWebArea ─────────────────────────
        // We use LegacyIAccessiblePattern::get_CurrentValue() on the RootWebArea.
        // Chrome populates this with the current page URL regardless of focus state.
        // get_CurrentName() gives us the correct per-tab page title.
        if(pRootWebArea)
        {
            std::wstring pageUrl;
            std::wstring pageTitle;

            // Get the page title from Name property
            BSTR bTitle = nullptr;
            if(SUCCEEDED(pRootWebArea->get_CurrentName(&bTitle)) && bTitle)
            {
                pageTitle = std::wstring(bTitle, SysStringLen(bTitle));
                SysFreeString(bTitle);
            }

            // Get the URL from LegacuIAccessible value
            IUIAutomationLegacyIAccessiblePattern* pLegacy = nullptr;
            if(SUCCEEDED(pRootWebArea->GetCurrentPattern(UIA_LegacyIAccessiblePatternId, (IUnknown**)&pLegacy)) && pLegacy)
            {
                BSTR bUrl = nullptr;
                if(SUCCEEDED(pLegacy->get_CurrentValue(&bUrl)) && bUrl)
                {
                    pageUrl = std::wstring(bUrl, SysStringLen(bUrl));
                    SysFreeString(bUrl);
                }
                pLegacy->Release();
            }

            if(!pageUrl.empty() || !pageTitle.empty())
            {
                // Store as a combined PageMeta section — Python will split it out
                // Format: "TITLE\n\nURL" so Python can split on \n\n
                std::wstring meta = pageTitle + L"\n\n" + pageUrl;
                UiaSection metaSec{
                    L"PageMeta",
                    L"",
                    meta
                };
                candidates.push_back(metaSec);
                std::wcout << L"\033[90m[ScreenReader] PageMeta: title='" << pageTitle << L"' url='" << pageUrl << L"'\033[0m\n";
            }
            pRootWebArea->Release();
            pRootWebArea = nullptr;
        }

        // ── 6. PURGE DOCUMENT IF MAIN IS FOUND ──
        if(foundMainLandmark)
        {
            candidates.erase(std::remove_if(candidates.begin(), candidates.end(),
                [](const UiaSection& s) { return s.typeName == L"Document"; }), 
                candidates.end());
        }
        
        //── COM cleanup (fix original leak: pRoot/pAutomation never freed) ─
        pRoot->Release();
        pAutomation->Release();

        if(candidates.empty()) return L"";

        // Sort by length descending — richest content first
        std::sort(candidates.begin(), candidates.end(),
            [](const UiaSection& a, const UiaSection& b)
        {
            // Edit (clean code) always before Document (noisy page text)
            auto priority = [](const std::wstring& t) -> int
            {
                if(t == L"Edit") return 0;
                if(t == L"Document") return 1;
                return 2;
            };
            int pa = priority(a.typeName), pb = priority(b.typeName);
            if(pa != pb) return pa < pb;
            return a.text.length() > b.text.length();
        });

        // Cap at 5 sections — but always keep the PageMeta
        {
            std::vector<UiaSection> urlSecs, contentSecs;
            for(auto& c : candidates)
                (c.typeName == L"PageMeta" ? urlSecs : contentSecs).push_back(c);
            if(contentSecs.size() > 5) contentSecs.resize(5);
            candidates = urlSecs; // PageMeta first
            candidates.insert(candidates.end(), contentSecs.begin(), contentSecs.end());
        }

        // ── Ghost Clipboard: override Edit section with full Monaco buffer ────────
        if(pMonacoEl)
        {
            std::cout << "\033[90m[ScreenReader] Ghost Clipboard: extracting full code buffer...\033[0m\n";
            std::wstring fullCode = GhostClipboard(pMonacoEl);
            pMonacoEl->Release();
            pMonacoEl = nullptr;

            if(!fullCode.empty())
            {
                // Sanity check: ghost result must be >= the UIA partial window.
                // If it's shorter, SetFocus hit the wrong element — use UIA fallback.

                bool valid = true;
                for(const auto& c : candidates)
                {
                    if(c.typeName == L"Edit" && fullCode.length() < c.text.length())
                    {
                        valid = false;
                        std::cout << "\033[33m[ScreenReader] Ghost Clipboard shorter than UIA — keeping UIA window text.\033[0m\n";
                        break;
                    }
                }

                if(valid)
                {
                    for(auto& c : candidates)
                    {
                        if(c.typeName == L"Edit")
                        {
                            c.text = fullCode;
                            c.fullBuffer = true;
                            std::cout << "\033[32m[ScreenReader] Ghost Clipboard: " << fullCode.length() << " chars (full Monaco buffer).\033[0m\n";
                            break; // only override the first Edit (Monaco code editor)
                        }
                    }
                }
            }
            else
                std::cout << "\033[33m[ScreenReader] Ghost Clipboard returned empty — using UIA window text.\033[0m\n";
        }
        // ─────────────────────────────────────────────────────────────────────────


        // ── Serialize to JSON array → Python reads with json.loads() ─────────────
        std::wstring json = L"[";
        bool first = true;
        for (size_t i = 0; i < candidates.size(); i++)
        {
            if(!first) json += L",";
            first = false;
            if(candidates[i].typeName == L"PageMeta")
            {
                // Split "title\n\nurl" back into two fields for Python
                std::wstring meta = candidates[i].text;
                size_t sep = meta.find(L"\n\n");
                std::wstring title = (sep != std::wstring::npos) ? meta.substr(0,sep) : meta;
                std::wstring url   = (sep != std::wstring::npos) ? meta.substr(sep + 2) : L"";
                json += L"{\"type\":\"PageMeta\",\"title\":\"" + JsonEscapeW(title)
                      + L"\",\"url\":\"" + JsonEscapeW(url) + L"\"}";
            }
            else
            {
                json += L"{\"type\":\"" + candidates[i].typeName;
                json += L"\",\"name\":\"" + JsonEscapeW(candidates[i].automationId);
                json += L"\",\"full_buffer\":" + std::wstring(candidates[i].fullBuffer ? L"true" : L"false");
                json += L",\"text\":\"" + JsonEscapeW(candidates[i].text) + L"\"}";
            }
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
            
            DWORD idleTime;
            DWORD timeSinceGhost = 0;
            if (lii.dwTime >= Jugnu::g_lastGhostClipboardInputTime) {
                timeSinceGhost = lii.dwTime - Jugnu::g_lastGhostClipboardInputTime;
            } else {
                timeSinceGhost = Jugnu::g_lastGhostClipboardInputTime - lii.dwTime;
            }

            // If the last OS input event matches our GhostClipboard timestamp (within 500ms),
            // it means the user hasn't touched the computer since we ran GhostClipboard.
            if (timeSinceGhost < 500) {
                // OS timer was hijacked by GhostClipboard. Force it to remain in the idle block.
                idleTime = 60000;
            } else {
                idleTime = GetTickCount() - lii.dwTime;
            }

            if(idleTime >= 60000)   // 60 seconds idle → capture screen
            {
                HWND hwnd = GetForegroundWindow();
                if(!hwnd)
                {
                    Sleep(2000);
                    continue;
                }
                std:: string currentApp = WinMonitor::GetProcessName(hwnd);
                std::string windowTitle = WinMonitor::GetWindowTextString(hwnd);

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
                        std::cout << "\033[1;36m[ScreenReader]\033[0m 60s idle detected in '" << currentApp << "' - reading screen context...\n";

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

                            if(DBHandler::BufferOCR(currentApp, windowTitle, utf8_text))
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
        // COM/WinRT cleanup: winrt::init_apartment() manages its own apartment lifetime.
        // The OS automatically cleans up the thread's COM state on thread exit.
        return 0;
    }
}