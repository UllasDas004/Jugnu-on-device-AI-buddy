#include "file_watcher.h"
#include "server/ipc_server.h"
#include<iostream>

namespace Jugnu
{
    std::atomic<bool> FileWatcher::isRunning{false};
    HANDLE FileWatcher::hThread = NULL;
    HANDLE FileWatcher::hDir = INVALID_HANDLE_VALUE;
    std::string FileWatcher::watchPath = "";

    void FileWatcher::Start(const std::string& directoryToWatch)
    {
        if(isRunning) return;
        watchPath = directoryToWatch;
        isRunning = true;
        hThread = CreateThread(NULL, 0, WatcherThread, NULL, 0, NULL);
    }

    void FileWatcher::Stop()
    {
        isRunning = false;
        if(hDir != INVALID_HANDLE_VALUE) CancelIoEx(hDir, NULL); // Wakes the sleeping ReadDirectoryChangesW call
        if(hThread)
        {
            WaitForSingleObject(hThread, 1000); // Wait for thread to gracefully exit
            CloseHandle(hThread);
            hThread = NULL;
        }
    }

    static std::string EscapeJSON(const std::string& input)
    {
        std::string output;
        for(char c : input)
        {
            if(c == '"') output += "\\\"";
            else if(c == '\\') output += "\\\\";
            else if(c == '\b') output += "\\b";
            else if(c == '\f') output += "\\f";
            else if(c == '\n') output += "\\n";
            else if(c == '\r') output += "\\r";
            else if(c == '\t') output += "\\t";
            else if (static_cast<unsigned char>(c) < 0x20) {
                char buf[7]; snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned char>(c)); output += buf;
            } else { output += c; }
        }
        return output;
    }

    DWORD WINAPI FileWatcher::WatcherThread(LPVOID lpParam)
    {
        std::cout<<"\033[1;35m[GhostWriter]\033[0m Watching directory: \033[4m" << watchPath << "\033[0m\n";

        // Open a handle to the directory we want to monitor
        hDir = CreateFileA(
            watchPath.c_str(),
            FILE_LIST_DIRECTORY,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            NULL,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            NULL
        );

        if(hDir == INVALID_HANDLE_VALUE)
        {
            std::cerr << "\033[1;35m[GhostWriter]\033[0m \033[1;31mFailed to open directory for watching.\033[0m\n";
            return 1;
        }

        char buffer[1024];
        DWORD bytesReturned;
        while(isRunning)
        {
            // This is a BLOCKING call. It puts the thread to sleep until a file changes!
            if(ReadDirectoryChangesW(
                hDir, buffer, sizeof(buffer),
                TRUE, // Watch subdirectories too
                FILE_NOTIFY_CHANGE_LAST_WRITE, // Only care when a file is saved
                &bytesReturned, NULL, NULL
            ))
            {
                FILE_NOTIFY_INFORMATION* fni = reinterpret_cast<FILE_NOTIFY_INFORMATION*>(buffer);

                do
                {
                    if(fni->Action == FILE_ACTION_MODIFIED)
                    {
                        // Convert the wide-string filename to a standard string
                        std::wstring wFilename(fni->FileName, fni->FileNameLength/sizeof(WCHAR));
                        std::string filename(wFilename.begin(), wFilename.end());

                        std::string absolutePath = watchPath + "\\" + filename;

                        // Ignore temporary files, metadata, and environment folders
                        if( filename.find(".git") == std::string::npos &&
                            filename.find(".venv") == std::string::npos &&
                            filename.find("build\\") == std::string::npos &&
                            filename.find(".tmp") == std::string::npos &&
                            filename.find(".db") == std::string::npos &&
                            filename.find(".pyc") == std::string::npos &&
                            filename.find("__pycache__") == std::string::npos &&
                            filename.find("pyproject.toml") == std::string::npos &&
                            filename.find("uv.lock") == std::string::npos
                        )
                        {
                            std::cout << "\033[1;35m[GhostWriter]\033[0m Detected change in: \033[4m" << absolutePath << "\033[0m\n";
                            std::cout << "\033[1;35m[GhostWriter]\033[0m User saved: \033[3m" << filename << "\033[0m. Sending to Python...\n";

                            std::string escapePath = EscapeJSON(absolutePath);
    
                            // Send the file change event to Python!
                            std::string payload = "{\"type\": \"FILE_SAVED\", \"file\": \"" + escapePath + "\"}";
                            Jugnu::IPCServer::SendMessageToPython(payload);
                        }
                    }
                    fni = fni->NextEntryOffset ? reinterpret_cast<FILE_NOTIFY_INFORMATION*>(reinterpret_cast<char*>(fni) + fni->NextEntryOffset) : nullptr;
                } while(fni);
            }
        }
        CloseHandle(hDir);
        return 0;
    }
}