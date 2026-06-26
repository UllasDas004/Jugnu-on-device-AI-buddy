cmake_minimum_required(VERSION 3.20)
project(Jugnu VERSION 1.0.0 LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Find packages provided by vcpkg
#find_package(nlohmann_json CONFIG REQUIRED) # Required for JSON parsing


# Core sources for Step 1 Skeleton
set(
    SOURCES
    src/main.cpp
    src/monitor/win_monitor.cpp
    src/server/ipc_server.cpp
    src/db/sqlite3.c
    src/db/sqlite-vec.c
    src/db/db_handler.cpp
    src/monitor/memory_manager.cpp
    src/monitor/file_watcher.cpp
    src/monitor/clipboard_manager.cpp
    src/server/flush_worker.cpp
)

add_executable(Jugnu ${SOURCES})

# Target include directories
target_include_directories(Jugnu PRIVATE src)

# Link libraries (Win32 APIs and vcpkg libs)
target_link_libraries(Jugnu PRIVATE
    # nlohmann_json::nlohmann_json
    User32      # Required for Win API calls like MessageBox, SetWinEventHook and windows APIs
    Advapi32    # For process information
    Psapi       # For reading process names (GetModuleBaseName)
)

