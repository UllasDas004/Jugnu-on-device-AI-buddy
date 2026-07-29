#pragma once
#include<string>
#include<unordered_map>
#include "db/sqlite3.h"

namespace Jugnu
{
    class DBHandler
    {
        public:
        // initialize the DB ad create tables
        static bool Init(const std::string& dbPath = "jugnu.db");
        // Safety close the database
        static void Cleanup();

        // Load permanent Markov edges from SQLite on boot
        static std::unordered_map<std::string, std::unordered_map<std::string, int>> LoadMarkovEdges();
        // Update the permanent Markov table
        static bool FlushMarkovEdges(const std::unordered_map<std::string, std::unordered_map<std::string, int>>& edges);

        // EMA Scores (Priority)
        static std::unordered_map<std::string, float> LoadEMAScores();
        static bool FlushEMAScores(const std::unordered_map<std::string, float>& scores);

        // Save the absolute path of an executable
        static bool UpsertAppPath(const std::string& processName, const std::string& absolutePath);
        // Retrieve the absolute path for RAM prefetching
        static std::string GetAppPath(const std::string& processName);
        
        // Write raw OCR text directly to the ocr_buffer staging table.
        // Python's FlushWorker reads this every 60s, cleans with Gemma, and vectorizes.
        // This bypasses the IPC pipe - large OCR blobs never travel over named Pipes.
        static bool BufferOCR(const std::string& appName, const std::string& windowTitle, const std::string& rawText);
        
        private:
        // Sqlite DB Connection Handle
        static sqlite3* db;
        // executing function wrapper
        static bool ExecuteSQL(const std::string& sql);
    };
}