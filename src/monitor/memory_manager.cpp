#include "memory_manager.h"
#include <iostream>
#include <algorithm>
#include "db/db_handler.h"
#include <unordered_set>
#include <cmath>

namespace Jugnu
{
    std::mutex MemoryManager::memMutex;
    std::string MemoryManager::lastApp = "";
    std::unordered_map<std::string, std::unordered_map<std::string, int>> MemoryManager::markovChain;
    std::unordered_map<std::string, std::unordered_map<std::string, int>> MemoryManager::historicalMarkovChain;
    std::list<std::string> MemoryManager::lruList;
    std::unordered_map<std::string, std::list<std::string>::iterator> MemoryManager::lruMap;
    std::unordered_map<std::string, float> MemoryManager::emaScores;

    void MemoryManager::Init()
    {
        std::cout << "\033[35m[Memory]\033[0m Memory Manager Initialized.\n";

        // Load history from DB into RAM for O(1) predictions
        historicalMarkovChain = Jugnu::DBHandler::LoadMarkovEdges();
        std::cout << "\033[35m[Memory]\033[0m Loaded " << historicalMarkovChain.size() << " historical Markov states.\n";

        // Load priority scores from DB
        emaScores = Jugnu::DBHandler::LoadEMAScores();
        std::cout << "\033[35m[Memory]\033[0m Loaded " << emaScores.size() << " App Priorities.\n";
    }
    
    void MemoryManager::Stop()
    {
        std::lock_guard<std::mutex> lock(memMutex);
        markovChain.clear();
        historicalMarkovChain.clear();
        lruList.clear();
        lruMap.clear();
        emaScores.clear();
        std::cout << "\033[35m[Memory]\033[0m Memory Manager shut down.\n";
    }
    
    
    void MemoryManager::ProcessAppSwitch(const std::string& processName, const std::string& windowTitle)
    {
        // Protect RAM with a mutex since IPC threads or Flush threads will access this later
        std::lock_guard<std::mutex> lock(memMutex);

        if(processName.empty()) return;

        // TRAP FIX: Ignore all Explorer.EXE windows (Taskbar, Alt-Tab overlay, File Explorer)
        // so it doesn't pollute the Markov Chain predictions!
        if(processName == "Explorer.EXE" || processName == "explorer.exe") return;

        if(!lastApp.empty() && lastApp != processName) UpdateMarkov(lastApp, processName);

        lastApp = processName;
        UpdateLRU(processName);
        UpdateEMA(processName);
    }

    void MemoryManager::UpdateMarkov(const std::string& currentApp, const std::string& nextApp)
    {
        markovChain[currentApp][nextApp]++;
        std::cout << "\033[35m[Memory]\033[0m Markov Chain Learned: " << currentApp << " -> " << nextApp << " (" << markovChain[currentApp][nextApp] << " times)\n";
    }

    void MemoryManager::UpdateLRU(const std::string& app)
    {
        // If it exists in the cache, remove it from its current position
        auto it = lruMap.find(app);
        if(it != lruMap.end()) lruList.erase(it->second);

        // Push to the front of the MRU (Most Recently Used) list
        lruList.push_front(app);
        lruMap[app] = lruList.begin();

        // Evict least recently used if we exceed capacity
        if(lruList.size() > LRU_CAPACITY)
        {
            std::string lruApp = lruList.back();
            lruMap.erase(lruApp);
            lruList.pop_back();
        }
    }
    
    std::vector<std::string> MemoryManager::GetPredictedNextApps()
    {
        std::lock_guard<std::mutex> lock(memMutex);
        std::vector<std::string> predictions;

        bool inSession = markovChain.find(lastApp) != markovChain.end();
        bool inHistory = historicalMarkovChain.find(lastApp) != historicalMarkovChain.end();

        if(lastApp.empty() || (!inSession && !inHistory)) return predictions;

        // Combine counts from History Map + 30-min Session Map
        std::unordered_map<std::string, int> combinedEdges;

        // 1. Weight the Past (History) Higher! (to prevent reset on short breaks)
        if(inHistory)
        {
            for(const auto& edge : historicalMarkovChain[lastApp])
            combinedEdges[edge.first] += edge.second;
        }

        // 2. Add the Current Session Count
        if(inSession)
        {
            for(const auto& edge : markovChain[lastApp])
            combinedEdges[edge.first] += edge.second;
        }

        // Sort the next apps by frequency count
        std::vector<std::pair<std::string, int>> sortedApps(
            combinedEdges.begin(),
            combinedEdges.end()
        );

        std::sort(sortedApps.begin(), sortedApps.end(),
            [](const std::pair<std::string, int>& a,const std::pair<std::string, int>& b)
            {
                return a.second > b.second; // Descending
            }
        );

        for(const auto& pair : sortedApps) predictions.push_back(pair.first);
        return predictions;
    }

    std::unordered_map<std::string, std::unordered_map<std::string, int>> MemoryManager::ExtractAndClearMarkovChain()
    {
        // Protect RAM extraction with the Mutex! If we copy while WinMonitor is writing, the app will crash.
        std::lock_guard<std::mutex> lock(memMutex);

        // Copy the current map
        auto edges = markovChain;

        // Absorb the 30-min buffer into our permanent RAM history so we don't forget it!
        for (const auto& sourceNode : markovChain)
        {
            for (const auto& targetNode : sourceNode.second)
                historicalMarkovChain[sourceNode.first][targetNode.first] += targetNode.second;
        }

        // CRITICAL: Clear RAM ONLY AFTER copy to prevent data loss
        markovChain.clear();

        return edges;
    }

    void MemoryManager::UpdateEMA(const std::string& currentApp)
    {
        if(currentApp.empty() || currentApp == "Explorer.EXE" || currentApp == "explorer.exe") return;

        // Current app gets bumped to 1.0
        emaScores[currentApp] = 1.0f;

        // Everyone else decays
        for(auto it = emaScores.begin(); it != emaScores.end(); )
        {
            if(it->first != currentApp)
            {
                it->second *= 0.8f;
            }

            // Purge dead apps from RAM to save memory
            if(it->second < 0.05f)
            {
                it = emaScores.erase(it);
            }
            else
            {
                ++it;
            }
        }

        // Evaluate priorities on every switch
        ThrottleDistractors(currentApp);
    }

    void MemoryManager::PrefetchToRAM(const std::string& processName)
    {
        // 1. Ask SQLite for the absolute path of the predicted app
        std::string absolutePath = Jugnu::DBHandler::GetAppPath(processName);
        if(absolutePath.empty()) return; // We don't know where it is yet

        // 2. Open a silent background handle to the Executable
        HANDLE hFile = CreateFileA(
            absolutePath.c_str(),
            GENERIC_READ,
            FILE_SHARE_READ,
            NULL,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            NULL
        );

        if(hFile != INVALID_HANDLE_VALUE)
        {
            // 3. Read a tiny 4KB chunk of the binary file!
            // This forces the Windows OS Kernel to pull the ENTIRE eecutable
            // from the slow SSD into the ultra-fast Standby RAM Cache!

            char buffer[4096];
            DWORD bytesRead;
            ReadFile(hFile, buffer, sizeof(buffer), &bytesRead, NULL);
            CloseHandle(hFile);

            std::cout<<"\033[94m[Prefetch]\033[0m Kernel forced to cache "<<processName<<" into RAM!\n";
        }
    }

    // P2-FIX: Prevent JSON injection from window titles containing " or \.
    // Example: window titled Say "Hello" would produce: "current_app": "Say "Hello""
    // which is invalid JSON that crashes Python's json.loads().
    static std::string JsonEscape(const std::string& s)
    {
        std::string out;
        out.reserve(s.size());
        for (char c : s) {
            if      (c == '"')  out += "\\\"";
            else if (c == '\\') out += "\\\\";
            else if (c == '\n') out += "\\n";
            else if (c == '\r') out += "\\r";
            else                out += c;
        }
        return out;
    }
    std::string MemoryManager::GenerateContextJSON(const std::string& currentApp)
    {
        // We do not lock the mutex here because this is called immediately after ProcessAppSwitch
        std::string json = "{\n";
        json += "  \"type\": \"SWITCH\",\n";
        json += "  \"current_app\": \"" + JsonEscape(currentApp) + "\",\n";
        
        json += "  \"predicted_next\": [";
        if(markovChain.find(currentApp) != markovChain.end())
        {
            std::vector<std::pair<std::string, int>> sortedApps(markovChain[currentApp].begin(), markovChain[currentApp].end());
            std::sort(sortedApps.begin(),sortedApps.end(),[](const auto& a,const auto& b) { return a.second > b.second; });

            for(size_t i=0;i<sortedApps.size() && i<3;i++)
            {
                json += "\"" + JsonEscape(sortedApps[i].first) + "\"";
                if(i < sortedApps.size()-1 && i<2) json += ", ";
            }

            // Prefetch the #1 prediction into RAM
            if(!sortedApps.empty()) PrefetchToRAM(sortedApps[0].first);
        }
        json += "],\n";

        json += "  \"ema_context\": {";
        bool first = true;
        for (const auto& [app, score] : emaScores) {
            if (!first) json += ", ";
            json += "\"" + JsonEscape(app) + "\": " + std::to_string(score);
            first = false;
        }
        json += "}\n";
        json += "}\n";
        
        return json;
    }
    

    void MemoryManager::ThrottleDistractors(const std::string& currentApp)
    {
        auto it = emaScores.find(currentApp);
        if(it == emaScores.end()) return;

        float currentScore = it->second;
        const float DEEP_WORK_THRESHOLD = 0.8f;
        const float DISTRACTOR_THRESHOLD = 0.2f;
        
        bool isDeepWork = (currentScore >= DEEP_WORK_THRESHOLD);

        // P1-FIX: Only snapshot the process list when the app actually changes.
        // CreateToolhelp32Snapshot() scans 150+ processes every call — 5-20ms.
        // Previously this ran on EVERY foreground event, causing the >1ms warnings.
        static std::string lastThrottledFor = "";
        if(currentApp == lastThrottledFor) return;
        
        static bool lastWasDeepWork = false;

        lastWasDeepWork = isDeepWork;
        lastThrottledFor = currentApp;

        static std::unordered_set<DWORD> throttledPids;

        // Scan ALL running processes and let EMA scores decide who to throttle
        HANDLE hSnapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if(hSnapshot == INVALID_HANDLE_VALUE) return;

        PROCESSENTRY32 pe32;
        pe32.dwSize = sizeof(PROCESSENTRY32);

        if(Process32First(hSnapshot, &pe32))
        {
            do
            {
                std::string exeName = pe32.szExeFile;
                // Skip ourselves, system processes, and the current focused app
                if(exeName == currentApp || exeName == "System" || 
                   exeName == "svchost.exe" || exeName == "explorer.exe") continue;

                // Only act on apps we have actually SEEN before (score > 0)
                // Unknown background processes (score == 0) are left alone entirely

                float score = 0.0f;
                auto scoreIt = emaScores.find(exeName);
                if(scoreIt != emaScores.end()) score = scoreIt->second;
                if(score == 0.0f) continue;
                
                bool isDistractor = (score < DISTRACTOR_THRESHOLD);
                
                // We need QUERY permission to read the current priority, and SET to change it.
                HANDLE hProcess = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_INFORMATION, FALSE, pe32.th32ProcessID);
                if(hProcess)
                {
                    DWORD currentPriority = GetPriorityClass(hProcess);
                    if(currentPriority != 0) // 0 means access denied or error
                    {
                        if(isDeepWork && isDistractor)
                        {
                            // Only throttle if it isn't already throttled
                            if(currentPriority != IDLE_PRIORITY_CLASS)
                            {
                                SetPriorityClass(hProcess, IDLE_PRIORITY_CLASS);
                                throttledPids.insert(pe32.th32ProcessID);
                                std::cout << "\033[33m[Governor]\033[0m Throttled " << exeName << " (EMA=" << score << ")\n";
                            }
                        }
                        else
                        {
                            // CRITICAL FIX: Only restore if WE explicitly throttled it!
                            // Chromium/Electron natively sets background renderers to IDLE_PRIORITY_CLASS.
                            // If we blindly check for IDLE_PRIORITY_CLASS, we fight with Chrome!
                            if(throttledPids.find(pe32.th32ProcessID) != throttledPids.end())
                            {
                                SetPriorityClass(hProcess, NORMAL_PRIORITY_CLASS);
                                throttledPids.erase(pe32.th32ProcessID);
                                std::cout << "\033[33m[Governor]\033[0m Restored  " << exeName << " (EMA=" << score << ")\n";
                            }
                        }
                    }
                    CloseHandle(hProcess);
                }
            } while(Process32Next(hSnapshot, &pe32));
        }
        CloseHandle(hSnapshot);
    }

    std::unordered_map<std::string, float> MemoryManager::GetEMAScores()
    {
        std::lock_guard<std::mutex> lock(memMutex);
        // Returns a copy of the scores. We DO NOT clear them, because EMA is a lifetime rolling average!
        return emaScores; 
    }
} // namespace Jugnu