#pragma once
#include<windows.h>
#include<iostream>
#include<atomic>
#include<string>

namespace Jugnu
{
    class FileWatcher
    {
        public:
            static void Start(const std::string& directoryToWatch);
            static void Stop();

        private:
            static std::atomic<bool> isRunning;
            static HANDLE hThread;
            static HANDLE hDir;
            static std::string watchPath;
            static DWORD WINAPI WatcherThread(LPVOID lpParam);
    };
}