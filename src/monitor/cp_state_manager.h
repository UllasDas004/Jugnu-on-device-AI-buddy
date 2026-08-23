#pragma once
#include <string>
#include <windows.h>
#include<atomic>
#include<thread>


namespace Jugnu
{
    enum CPState
    {
        CPS_NONE,
        CPS_READING,
        CPS_CODING
    };

    class CPStateManager
    {
        public:
        static void Init();
        static void Cleanup();
        static void StartSession(const std::string& problemSlug, const std::string& platform);
        static void EndSession();
        static void OnStandbyEntered();
        static void OnStandbyExited();
        static void UpdateTimers();
        static void NotifyTypingResumed();
        static void OnKeyDown();

        private:
        static std::atomic<CPState> currentState;
        static DWORD lastKeyboardInputMs;
        static bool hasTriggeredReadingIdle;
        static bool hasTriggeredStuck;
        static bool isStandbyActive;
        static DWORD standbyStartTimeMs;
        static std::string activeCPSessionSlug;
    };
}