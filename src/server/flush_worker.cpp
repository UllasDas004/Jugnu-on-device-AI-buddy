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
    std::mutex FlushWorker::_cvMutex;
    std::condition_variable FlushWorker::_cv;

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
        _cv.notify_all();  // P2-FIX: Wake the sleeping thread immediately
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
            // P2-FIX: Use a condition variable instead of 1800 x 1-sec sleep.
            // Stop() can now wake this thread instantly instead of waiting up to 1s.
            {
                std::unique_lock<std::mutex> lk(_cvMutex);
                _cv.wait_for(lk, std::chrono::minutes(30), [&]{ return !isRunning.load(); });
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