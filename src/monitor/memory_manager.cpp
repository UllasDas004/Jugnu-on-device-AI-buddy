#include "memory_manager.h"
#include <iostream>
#include <algorithm>
#include "db/db_handler.h"

namespace Jugnu
{
    std::mutex MemoryManager::memMutex;
    std::string MemoryManager::lastApp = "";
    std::unordered_map<std::string, std::unordered_map<std::string, int>> MemoryManager::markovChain;
    std::list<std::string> MemoryManager::lruList;
    std::unordered_map<std::string, std::list<std::string>::iterator> MemoryManager::lruMap;
    std::unordered_map<std::string, float> MemoryManager::emaScores;

    void MemoryManager::Init()
    {
        std::cout << "\033[35m[Memory]\033[0m Memory Manager Initialized.\n";
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
        ThrottleDistractors(processName);
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

        if(lastApp.empty() || markovChain.find(lastApp) == markovChain.end()) return predictions;

        // Sort the next apps by frequency count
        std::vector<std::pair<std::string, int>> sortedApps(
            markovChain[lastApp].begin(),
            markovChain[lastApp].end()
        );

        std::sort(sortedApps.begin(), sortedApps.end(),
            [](const std::pair<std::string, int>& a,const std::pair<std::string, int>& b)
            {
                return a.second < b.second; // Descending
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

        // CRITICAL: Clear RAM ONLY AFTER copy to prevent data loss
        markovChain.clear();

        return edges;
    }

    void MemoryManager::UpdateEMA(const std::string& currentApp)
    {
        emaScores[currentApp] = 1.0f; // Boost current app

        // Decay all others
        for(auto it=emaScores.begin();it!= emaScores.end();)
        {
            if(it->first != currentApp)
            {
                it->second *= 0.8f;

                // TRAP FIX: Subnormal Float slowdown
                if(it->second < 0.1f)
                {
                    it = emaScores.erase(it);
                    continue;
                }
            }
            ++it;
        }
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

    std::string MemoryManager::GenerateContextJSON(const std::string& currentApp)
    {
        // We do not lock the mutex here because this is called immediately after ProcessAppSwitch
        std::string json = "{\n";
        json += "  \"type\": \"SWITCH\",\n";
        json += "  \"current_app\": \"" + currentApp + "\",\n";
        
        json += "  \"predicted_next\": [";
        if(markovChain.find(currentApp) != markovChain.end())
        {
            std::vector<std::pair<std::string, int>> sortedApps(markovChain[currentApp].begin(), markovChain[currentApp].end());
            std::sort(sortedApps.begin(),sortedApps.end(),[](const auto& a,const auto& b) { return a.second > b.second; });

            for(size_t i=0;i<sortedApps.size() && i<3;i++)
            {
                json += "\"" + sortedApps[i].first + "\"";
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
            json += "\"" + app + "\": " + std::to_string(score);
            first = false;
        }
        json += "}\n";
        json += "}\n";
        
        return json;
    }
    

    void MemoryManager::ThrottleDistractors(const std::string& currentApp)
    {
        // Define what constitutes a "Deep Work" app
        bool isDeepWork = (currentApp == "code.exe" || currentApp == "devenv.exe" || currentApp == "pwsh.exe");

        // If we are deeo working, choke the ditractors. If we switch to a distractor, restore it!
        DWORD newPriority = isDeepWork ? IDLE_PRIORITY_CLASS : NORMAL_PRIORITY_CLASS;

        // Take a snapshot of every process running on the OS right now
        HANDLE hSnapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if(hSnapshot == INVALID_HANDLE_VALUE) return;

        PROCESSENTRY32 pe32;
        pe32.dwSize = sizeof(PROCESSENTRY32);

        if(Process32First(hSnapshot, &pe32))
        {
            do
            {
                std::string exeName = pe32.szExeFile;
                // Add your common distactors here
                if(exeName == "Discord.exe" || exeName == "Spotify.exe" || exeName == "slack.exe")
                {
                    HANDLE hProcess = OpenProcess(PROCESS_SET_INFORMATION, FALSE, pe32.th32ProcessID);
                    if(hProcess)
                    {
                        SetPriorityClass(hProcess, newPriority);
                        CloseHandle(hProcess);

                        if(isDeepWork) std::cout << "\033[33m[Governor]\033[0m Throttled CPU Priority for " << exeName << " to save cache.\n";
                        else std::cout << "\033[33m[Governor]\033[0m Restored CPU Priority for " << exeName << ".\n";
                    }
                }
            } while(Process32Next(hSnapshot, &pe32));
        }
        CloseHandle(hSnapshot);
    }
} // namespace Jugnu