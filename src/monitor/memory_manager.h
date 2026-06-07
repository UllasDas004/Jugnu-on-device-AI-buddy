#pragma once
#include<windows.h>
#include<string>
#include<unordered_map>
#include<list>
#include<vector>
#include<mutex>
#include<TlHelp32.h>

namespace Jugnu
{
    class MemoryManager
    {
        public:
        static void Init();

        // Called by WinMonitor every time the user switches apps
        static void ProcessAppSwitch(const std::string& processName, const std::string& windowTitle);

        // Get the top most probable next applications based on Markov chain
        static std::vector<std::string> GetPredictedNextApps();
        static std::unordered_map<std::string, std::unordered_map<std::string, int>> ExtractAndClearMarkovChain();

        static std::string GenerateContextJSON(const std::string& currentApp);

        private:
        // TRAP FIX: Protext our RAM maps from thread collisions
        static std::mutex memMutex;
        static std::string lastApp;

        // Markov Chain map: map<CurrentApp,map<NextApp, Count>>
        static std::unordered_map<std::string, std::unordered_map<std::string,int>> markovChain;

        // LRU Cache for priority
        static std::list<std::string> lruList;
        static std::unordered_map<std::string, std::list<std::string>::iterator> lruMap;
        static const size_t LRU_CAPACITY = 20;

        static void UpdateMarkov(const std::string& currentApp, const std::string& nextApp);
        static void UpdateLRU(const std::string& app);

        static std::unordered_map<std::string, float> emaScores;
        static void UpdateEMA(const std::string& currentApp);
        static void PrefetchToRAM(const std::string& processName);

        // Throttles background distractors based on current app
        static void ThrottleDistractors(const std::string& currentApp);

    };
}