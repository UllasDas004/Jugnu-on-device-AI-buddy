#pragma once
#include<thread>
#include<atomic>
#include<unordered_map>

namespace Jugnu
{
    class FlushWorker
    {
        public:
        // Starts the background thread
        static void Start();

        // Safely stops the background thread
        static void Stop();

        private:
        // Signal flag for the worker thread
        static std::atomic<bool> isRunning;

        // The actual worker thread object
        static std::thread workerThread;

        // The function that runs in the background
        static void RunLoop();
        
        // Periodically pushes the data from RAM to SQLite
        static void FlushToDatabase();

        // TRAP FIX: The Batter Drain Check
        static bool IsOnBattery();
    };
}