#include "db_handler.h"
#include<iostream>

// --- ADD THIS BLOCK ---
// We manually declare the C-function so we don't need to include sqlite-vec.h
// which breaks standard SQLite functions by redefining them as macros.
extern "C" {
    int sqlite3_vec_init(sqlite3 *db, char **pzErrMsg, const void *pApi);
}
// ----------------------



namespace Jugnu
{
    sqlite3* DBHandler::db = nullptr;

    bool DBHandler::Init(const std::string& dbPath)
    {
        // TRAP FIX: SQLite Thread Corruption.
        // We must force SQLite to be thread-safe before opening it.
        sqlite3_config(SQLITE_CONFIG_SERIALIZED);

        // TRAP FIX: Register sqlite-vec natively before openning!
        sqlite3_auto_extension((void(*)(void))sqlite3_vec_init);

        // Open the database with the FULLMUTEX flag ti serialize concurrent access
        int rc = sqlite3_open_v2(
            dbPath.c_str(),
            &db,
            SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
            NULL
        );
        if(rc != SQLITE_OK)
        {
            std::cerr<<"[DB] Cannot open database: "<<sqlite3_errmsg(db)<<"\n";
            return false;
        }

        // P0-FIX: Enable WAL mode for concurrent C++/Python access.
        // Without this, every C++ write locks the entire file, causing Python
        // SQLITE_BUSY even with timeout=5.0 during long flushes.
        // NORMAL synchronous is safe with WAL and ~3x faster than FULL.
        ExecuteSQL("PRAGMA journal_mode=WAL;");
        ExecuteSQL("PRAGMA synchronous=NORMAL;");



        // Create the App Priorities Table
        std::string create_priorities_sql = R"(
            CREATE TABLE IF NOT EXISTS app_priorities(
                process_name TEXT PRIMARY KEY,
                ema_score REAL NOT NULL
            );
        )";
        if(!ExecuteSQL(create_priorities_sql)) return false;

        // Create the Markov Edges Table
        std::string create_markov_edges_sql = R"(
            CREATE TABLE IF NOT EXISTS markov_edges(
                source_app TEXT,
                target_app TEXT,
                transition_count INTEGER DEFAULT 0,
                PRIMARY KEY(source_app, target_app)    
            );
        )";
        if(!ExecuteSQL(create_markov_edges_sql)) return false;

        // Create the app_path table
        std::string create_app_paths_sql = R"(
            CREATE TABLE IF NOT EXISTS app_paths(
                process_name TEXT PRIMARY KEY,
                absolute_path TEXT NOT NULL
            );
        )";
        if(!ExecuteSQL(create_app_paths_sql)) return false;

        // Create the standard metadata table for memories
        std::string create_episodic_sql = R"(
            CREATE TABLE IF NOT EXISTS episodic_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_name TEXT NOT NULL,
                window_title TEXT NOT NULL,
                file_path TEXT,
                text_content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        )";
        if(!ExecuteSQL(create_episodic_sql)) return false;



        // Create the Virtual Table for the vectors (384 dimensions for e5-small)
        // Why Virtual?
        // This is an in-memory index.
        // It is extremely fast and uses 100x less RAM than keeping the vectors in the regular table.
        // 384 is the dimension of the e5-small-v2 embedding model.
        std::string create_vec_sql = R"(
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_episodic USING vec0(
                embedding float[384]
            );
        )";
        if(!ExecuteSQL(create_vec_sql)) return false;

        // STaging table for raw OCR captures.
        // C++ writes here directly. Python's FlishWorker reads, cleans with Gemma, then vectorizes.
        std::string create_ocr_buffer_sql = R"(
            CREATE TABLE IF NOT EXISTS ocr_buffer (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                app_name  TEXT NOT NULL,
                window_title TEXT NOT NULL,
                raw_text  TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        )";
        if(!ExecuteSQL(create_ocr_buffer_sql)) return false;
        
        // OKF-inspired structured knowledge store.
        // Each row is a synthesized JSON knowledge document from one OCR capture.
        // Related captures of the same topic are MERGED here, not duplicated.

        std::string create_knowledge_docs_sql = R"(
            CREATE TABLE IF NOT EXISTS knowledge_docs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                topic         TEXT NOT NULL,
                source_app    TEXT,
                source_type   TEXT DEFAULT 'browser',    -- 'ide' or 'browser'
                file_path     TEXT,                      -- only set for ide captures
                source_url    TEXT,                      -- only set for browser captures
                window_title  TEXT,                      -- exact window title for merging
                summary       TEXT,                      -- compact prose used as embedding anchor
                tags          TEXT,                      -- JSON array of tags
                notes         TEXT,                      -- Notes, constraints, edges cases
                content       TEXT,                      -- Problem statements / documentation
                code_snippet  TEXT,                      -- Verbatim code
                first_seen    DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_updated  DATETIME DEFAULT CURRENT_TIMESTAMP,
                capture_count INTEGER DEFAULT 1
            );
        )";
        if(!ExecuteSQL(create_knowledge_docs_sql)) return false;
        // Vector index for semantic topic search (for merge detection).
        std::string create_vec_knowledge_sql = R"(
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_knowledge USING vec0(
                embedding float[384]
            );
        )";
        if(!ExecuteSQL(create_vec_knowledge_sql)) return false;

        // Practice mode: tracks per-problem hint progression state.
        // One row = one question per session.
        // A "session" expires when last_seen > 2h ago OR is_solved = 1.
        // hint_level 0-3 controls how much Gemma reveals each stuck trigger.
        std::string create_practice_sessions_sql = R"(
            CREATE TABLE IF NOT EXISTS practice_sessions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_slug      TEXT NOT NULL,
                platform          TEXT NOT NULL,
                session_start     DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen         DATETIME DEFAULT CURRENT_TIMESTAMP,
                hint_level        INTEGER DEFAULT 0,
                last_hint_text    TEXT,
                code_snapshot     TEXT,
                detected_approach TEXT,
                stuck_count       INTEGER DEFAULT 0,
                is_solved         INTEGER DEFAULT 0
            );
        )";
        if(!ExecuteSQL(create_practice_sessions_sql)) return false;
        
        return true;
    }

    void DBHandler::Cleanup()
    {
        if(db)
        {
            sqlite3_close(db);
            db = nullptr;
            std::cout<<"\033[1;33m[DB]\033[0m SQLite Database Closed.\n";
        }
    }



    bool DBHandler::ExecuteSQL(const std::string& sql)
    {
        char* errMsg = 0;
        int rc = sqlite3_exec(db, sql.c_str(), 0, 0, &errMsg);
        if(rc != SQLITE_OK)
        {
            std::cerr<<"[DB] SQL error: "<<errMsg<<"\n";
            sqlite3_free(errMsg);
            return false;
        }
        return true;
    }
    
    std::unordered_map<std::string, std::unordered_map<std::string, int>> DBHandler::LoadMarkovEdges()
    {
        std::unordered_map<std::string, std::unordered_map<std::string, int>> edges;
        if(!db) return edges;

        std::string sql = "SELECT source_app, target_app, transition_count FROM markov_edges;";
        sqlite3_stmt* stmt;

        if(sqlite3_prepare_v2(db, sql.c_str(), -1, &stmt, nullptr) == SQLITE_OK)
        {
            while(sqlite3_step(stmt) == SQLITE_ROW)
            {
                std::string source = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 0));
                std::string target = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1));
                int count = sqlite3_column_int(stmt, 2);
                edges[source][target] = count;
            }
            sqlite3_finalize(stmt);
        }
        return edges;
    }

    bool DBHandler::FlushMarkovEdges(const std::unordered_map<std::string, std::unordered_map<std::string,int>>& edges)
    {
        if(!db) return false;

        // TRAP FIX: Use a transaction for bulk inserts (100x performance boost!)
        ExecuteSQL("BEGIN TRANSACTION;");

        sqlite3_stmt* stmt;
        std::string sql = R"(
            INSERT INTO markov_edges (source_app, target_app, transition_count)
            VALUES (?, ?, ?)
            ON CONFLICT(source_app, target_app)
            DO UPDATE SET transition_count = transition_count + excluded.transition_count;
        )";

        int rc = sqlite3_prepare_v2(db, sql.c_str(), -1, &stmt, NULL);
        if(rc != SQLITE_OK)
        {
            ExecuteSQL("ROLLBACK;");
            return false;
        }

        // Loop through the matrix and bind variables
        for(const auto& [source, targets] : edges)
        {
            for(const auto& [target, count] : targets)
            {
                sqlite3_bind_text(stmt, 1, source.c_str(), -1, SQLITE_TRANSIENT);
                sqlite3_bind_text(stmt, 2, target.c_str(), -1, SQLITE_TRANSIENT);
                sqlite3_bind_int(stmt, 3, count);

                sqlite3_step(stmt);
                sqlite3_reset(stmt); // Reset the statement for the next iteration
            }
        }

        sqlite3_finalize(stmt);
        ExecuteSQL("COMMIT;");
        return true;
    }



    std::unordered_map<std::string, float> DBHandler::LoadEMAScores()
    {
        std::unordered_map<std::string, float> scores;
        if(!db) return scores;

        std::string sql = "SELECT process_name, ema_score FROM app_priorities;";
        sqlite3_stmt* stmt;
        if(sqlite3_prepare_v2(db, sql.c_str(), -1, &stmt, nullptr) == SQLITE_OK)
        {
            while(sqlite3_step(stmt) == SQLITE_ROW)
            {
                std::string process = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 0));
                float score = static_cast<float>(sqlite3_column_double(stmt, 1));
                scores[process] = score;
            }
            sqlite3_finalize(stmt);
        }
        return scores;
    }

    bool DBHandler::FlushEMAScores(const std::unordered_map<std::string, float>& scores)
    {
        if(!db) return false;
        ExecuteSQL("BEGIN TRANSACTION;");

        // Clear the old priorities. Since RAM is the source of truth (loaded on startup),
        // wiping the table ensures that apps which decayed < 0.05 and were purged from RAM
        // are also correctly removed from the database.
        ExecuteSQL("DELETE FROM app_priorities;");

        // Insert the fresh scores
        std::string sql = R"(
            INSERT INTO app_priorities (process_name, ema_score) VALUES (?, ?);
        )";

        sqlite3_stmt* stmt;
        if(sqlite3_prepare_v2(db, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK)
        {
            ExecuteSQL("ROLLBACK;");
            return false;
        }

        for(const auto& [process, score] : scores)
        {
            sqlite3_bind_text(stmt, 1, process.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_double(stmt, 2, score);
            sqlite3_step(stmt);
            sqlite3_reset(stmt);
        }
        sqlite3_finalize(stmt);
        ExecuteSQL("COMMIT;");
        return true;
    }

    bool DBHandler::UpsertAppPath(const std::string& processName, const std::string& absolutePath)
    {
        if(!db) return false;

        sqlite3_stmt* stmt;
        std::string sql = R"(
            INSERT INTO app_paths (process_name, absolute_path)
            VALUES (?, ?)
            ON CONFLICT(process_name)
            DO UPDATE SET absolute_path = excluded.absolute_path;
        )";

        if(sqlite3_prepare_v2(db, sql.c_str(), -1, &stmt, NULL) == SQLITE_OK)
        {
            sqlite3_bind_text(stmt, 1, processName.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 2, absolutePath.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_step(stmt);
            sqlite3_finalize(stmt);
            return true;
        }
        return false;
    }

    std::string DBHandler::GetAppPath(const std::string& processName)
    {
        if(!db) return "";

        sqlite3_stmt* stmt;
        std::string sql = "SELECT absolute_path FROM app_paths WHERE process_name = ?;";
        std::string result = "";

        if(sqlite3_prepare_v2(db, sql.c_str(), -1, &stmt, NULL) == SQLITE_OK)
        {
            sqlite3_bind_text(stmt, 1, processName.c_str(), -1, SQLITE_TRANSIENT);
            if(sqlite3_step(stmt) == SQLITE_ROW)
            {
                result = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 0));
            }

            sqlite3_finalize(stmt);
        }
        return result;
    }

    bool DBHandler::BufferOCR(const std::string& appName, const std::string& windowTitle, const std::string& rawText)
    {
        if(!db) return false;

        // Use a prepared statement so special characters in OCR text
        // (quotes, backslashes, unicode) are handled safely by SQLite.
        // No manual escaping needed — that was only required for the JSON IPC pipe.

        sqlite3_stmt* stmt;
        const char* sql = "INSERT INTO ocr_buffer (app_name, window_title, raw_text) VALUES (?, ?, ?);";
        int rc = sqlite3_prepare_v2(db, sql, -1, &stmt, NULL);
        if(rc != SQLITE_OK) return false;

        sqlite3_bind_text(stmt, 1, appName.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(stmt, 2, windowTitle.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(stmt, 3, rawText.c_str(), -1, SQLITE_TRANSIENT);

        rc = sqlite3_step(stmt);
        sqlite3_finalize(stmt);

        return (rc == SQLITE_DONE);
    }
}