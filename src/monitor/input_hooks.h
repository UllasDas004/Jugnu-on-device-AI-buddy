#pragma once
#include<windows.h>
#include<atomic>

namespace Jugnu
{
    // Shared state between the hook thread and the rest of the app
    extern std::atomic<DWORD> g_lastKeyboardInputMs;
    extern std::atomic<DWORD> g_lastMouseInputMs;
    extern std::atomic<bool> g_isMouseOnly;
    extern std::atomic<int> g_cpKeyStrokeCount;

    // Public API for the CP State Manager
    void StartCPInputHooks();
    void StopCPInputHooks();
}