#include "server/flush_worker.h"
#include "db/db_handler.h"
#include "monitor/memory_manager.h"
#include <windows.h>
#include <iostream>
#include <chrono>

namespace Jugnu
{
    std::atomic<bool> FlushWorker::isRunning(false);
    std::thread FlushWorker::workerThread;

    void FlushWorker::Start()
    {
        // Only start if not already running
        if(isRunning) return;

        // Set the atomic flag to true to signal the loop to start
        isRunning = true;

        // Create and launch the background thread
        workerThread = std::thread(RunLoop);
        std::cout << "[FlushWorker] Background Consolidator Thread Started.\n";
    }

    void FlushWorker::Stop()
    {
        // Only stop if running
        if(!isRunning) return;

        // Safely stop the thread
        isRunning = false;
        // Check if thread is joinable and then join it
        if(workerThread.joinable()) workerThread.join();
    }

    bool FlushWorker::IsOnBattery()
    {
        SYSTEM_POWER_STATUS status;
        if (GetSystemPowerStatus(&status)) {
            return status.ACLineStatus == 0; // 0 means offline (on battery)
        }
        return false;
    }

    void FlushWorker::RunLoop()
    {
        // Infinite loop that only breaks when Stop() is called
        while(isRunning)
        {
            // PRODUCTION PATCH: Sleep for 30 minutes (1800 seconds) in 1-second chunks so it can stop cleanly
            for(int i=0; i<1800 && isRunning; i++) {
                std::this_thread::sleep_for(std::chrono::seconds(1));
            }

            if(!isRunning) break;

            // TRAP FIX: Battery Drain Check
            if(IsOnBattery())
            {
                std::cout << "[FlushWorker] Laptop is on battery. Skipping heavy SQL flush to save power.\n";
                continue; // Skip to next loop iteration
            }
            
            FlushToDatabase();
        }
    }

    void FlushWorker::FlushToDatabase()
    {
        std::cout << "[FlushWorker] Waking up. Consolidating Markov scores to SQLite...\n";

        // 1. Extract and clear the RAM matrix
        auto edges = Jugnu::MemoryManager::ExtractAndClearMarkovChain();

        if(!edges.empty())
        {
            // 2. Push edge counts into the permanent SQL table
            Jugnu::DBHandler::FlushMarkovEdges(edges);
        }

        // 3. Delete the raw logs to save space
        Jugnu::DBHandler::ClearAppLogs();

        std::cout << "[FlushWorker] Memory Flush Complete. Going back to sleep.\n\n";
    }
}