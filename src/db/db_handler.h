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

        // Save a raw window switch event
        static bool LogAppSwitch(const std::string& processName, const std::string& windowTitle);

        // Update the permanent Markov table
        static bool FlushMarkovEdges(const std::unordered_map<std::string, std::unordered_map<std::string, int>>& edges);
        // Delete raw logs to save space
        static bool ClearAppLogs();

        // Save the absolute path of an executable
        static bool UpsertAppPath(const std::string& processName, const std::string& absolutePath);
        // Retrieve the absolute path for RAM prefetching
        static std::string GetAppPath(const std::string& processName);
        
        private:
        // Sqlite DB Connection Handle
        static sqlite3* db;
        // executing function wrapper
        static bool ExecuteSQL(const std::string& sql);
    };
}