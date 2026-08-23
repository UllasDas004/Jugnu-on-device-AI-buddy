#include "cp_state_manager.h"
#include "input_hooks.h"
#include "win_monitor.h"
#include "server/ipc_server.h"
#include "screen_reader.h"
#include <iostream>

namespace Jugnu
{
    std::atomic<CPState> CPStateManager::currentState = CPS_NONE;
    DWORD CPStateManager::lastKeyboardInputMs = 0;
    bool CPStateManager::hasTriggeredReadingIdle = false;
    bool CPStateManager::hasTriggeredStuck = false;
    bool CPStateManager::isStandbyActive = false;
    DWORD CPStateManager::standbyStartTimeMs = 0;
    std::string CPStateManager::activeCPSessionSlug = "";

    void CPStateManager::Init()
    {
        lastKeyboardInputMs = GetTickCount();
    }

    void CPStateManager::Cleanup()
    {
        Jugnu::StopCPInputHooks();
    }

    void CPStateManager::StartSession(const std::string& problemSlug, const std::string& platform)
    {
        if(currentState != CPS_NONE)
        {
            if(activeCPSessionSlug == problemSlug) return;
            EndSession();
        }

        currentState = CPS_READING;
        activeCPSessionSlug = problemSlug;
        lastKeyboardInputMs = GetTickCount();
        hasTriggeredReadingIdle = false;
        hasTriggeredStuck = false;

        Jugnu::StartCPInputHooks();

        std::string payload = "{\"type\": \"CP_SESSION_START\", \"slug\": \"" + problemSlug + "\", \"platform\": \"" + platform + "\"}";
        Jugnu::IPCServer::SendMessageToPython(payload);
        std::cout << "\033[1;36m[CPState]\033[0m Started CP Session for: " << problemSlug << "\n";

        // Capture the initial code state the moment a session opens.
        // Small delay lets the browser render the code editor before we extract.
        // Fires asynchronously so StartSession doesn't block the WinEvent thread.
        std::thread([]{
            Sleep(800); // 800ms: enough for LeetCode's Monaco editor to finish rendering
            ScreenReader::TriggerGhostClipboard();
            std::cout << "\033[1;36m[CPState]\033[0m Initial GhostClipboard fired for code snapshot.\n";
        }).detach();
    }

    void CPStateManager::EndSession()
    {
        if (currentState != CPS_NONE)
        {
            Jugnu::StopCPInputHooks();

            std::cout << "\033[1;36m[CPState]\033[0m Ending CP Session.\n";
            currentState = CPS_NONE;
            activeCPSessionSlug = "";
            hasTriggeredReadingIdle = false;
            hasTriggeredStuck = false;
            std::string escapedCode = Jugnu::ScreenReader::GetLastCodeBufferJsonEscaped();
            Jugnu::IPCServer::SendMessageToPython("{\"type\": \"CP_SESSION_END\", \"code\": \"" + escapedCode + "\"}");
        }
    }

    void CPStateManager::OnKeyDown()
    {
        lastKeyboardInputMs = GetTickCount();
        if(currentState == CPS_READING)
        {
            std::cout << "\033[1;36m[CPState]\033[0m User started typing! State -> CPS_CODING.\n";
            currentState = CPS_CODING;
            hasTriggeredStuck = false;
        }
        if(currentState == CPS_CODING && hasTriggeredStuck) NotifyTypingResumed();
    }

    void CPStateManager::OnStandbyEntered()
    {
        if(currentState == CPS_NONE || isStandbyActive) return;

        isStandbyActive = true;
        standbyStartTimeMs = GetTickCount();
        std::cout << "\033[1;33m[CPState]\033[0m Entered Standby (Non-Whitelisted App).\n";
    }

    void CPStateManager::OnStandbyExited()
    {
        if (isStandbyActive)
        {
            isStandbyActive = false;
            if (GetTickCount() - standbyStartTimeMs > 10000)
            {
                std::cout << "\033[1;31m[CPState]\033[0m User was in Standby for >10s. Session called off.\n";
                EndSession();
            }
            else
            {
                std::cout << "\033[1;32m[CPState]\033[0m Returned from Standby within 10s. Session retained.\n";
            }
        }
    }

    void CPStateManager::UpdateTimers()
    {
        if(currentState == CPS_NONE) return;
        if(Jugnu::WinMonitor::g_isJugnuUIFocused) return;
        DWORD idleTime = GetTickCount() - lastKeyboardInputMs;

        // ── 60s READING IDLE: spawn nudge bubble ────────────────────────────────
        if(currentState == CPS_READING && !hasTriggeredReadingIdle)
        {
            if(idleTime > 60000)
            {
                std::string escapedCode = ScreenReader::GetLastCodeBufferJsonEscaped();
                Jugnu::IPCServer::SendMessageToPython("{\"type\": \"CP_READING_IDLE\", \"code\": \"" + escapedCode + "\"}");
                hasTriggeredReadingIdle = true;
                std::cout << "\033[1;36m[CPState]\033[0m 60s reading idle — nudge bubble sent.\n";
            }
        }

        // ── GHOST CLIPBOARD: only relevant while coding ─────────────────────────
        if(currentState == CPS_CODING)
        {
            int keystrokes = Jugnu::g_cpKeyStrokeCount.load();
            if(keystrokes >= 50 && idleTime >= 5000)
            {
                Jugnu::g_cpKeyStrokeCount = 0;
                std::cout << "\033[1;36m[CPState]\033[0m Safe idle after 50 keystrokes. Triggering GhostClipboard...\n";
                ScreenReader::TriggerGhostClipboard();
            }
        }

        // ── 180s STUCK: fires in BOTH CPS_READING and CPS_CODING ────────────────
        // In CPS_READING: only arm after the 60s nudge bubble has already been sent
        // (hasTriggeredReadingIdle == true), so we don't skip from 0→stuck with no bubble.
        bool readingStuckArmed = (currentState == CPS_READING && hasTriggeredReadingIdle);
        bool codingStuckArmed  = (currentState == CPS_CODING);

        if((readingStuckArmed || codingStuckArmed) && !hasTriggeredStuck)
        {
            if(idleTime > 180000)
            {
                std::cout << "\033[1;36m[CPState]\033[0m Stuck for 180s. Triggering final GhostClipboard before prompt...\n";
                ScreenReader::TriggerGhostClipboard();

                std::string escapedCode = ScreenReader::GetLastCodeBufferJsonEscaped();
                Jugnu::IPCServer::SendMessageToPython("{\"type\": \"CP_STUCK\", \"code\": \"" + escapedCode + "\"}");
                hasTriggeredStuck = true;
            }
        }
    }

    void CPStateManager::NotifyTypingResumed()
    {
        std::cout << "\033[1;33m[CPState]\033[0m User resumed typing! Sending cancellation.\n";
        Jugnu::IPCServer::SendMessageToPython("{\"type\": \"CP_USER_RESUMED\"}");
        hasTriggeredStuck = false; // Re-arm the stuck timer
    }
}
