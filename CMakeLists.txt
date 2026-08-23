cmake_minimum_required(VERSION 3.20)
project(Jugnu VERSION 1.0.0 LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
# Static runtime — single .exe, no DLL deps




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
    src/monitor/screen_reader.cpp
    src/monitor/cp_state_manager.cpp
    src/monitor/input_hooks.cpp
    src/server/flush_worker.cpp
)

add_executable(Jugnu ${SOURCES})

# Target include directories
target_include_directories(Jugnu PRIVATE src)

# Link libraries (Win32 APIs and vcpkg libs)
target_link_libraries(Jugnu PRIVATE
    User32      # Required for Win API calls like MessageBox, SetWinEventHook and windows APIs
    Advapi32    # For process information
    Psapi       # For reading process names (GetModuleBaseName)
    windowsapp  # WinRT runtime (Windows.Media.Ocr, etc.)
    UIAutomationCore    # Windows UI Automation — reads live text from Chrome, VS Code, etc.
)

# Add this if sqlite-vec fails to compile with MSVC
if(MSVC)
    set_source_files_properties(src/db/sqlite-vec.c PROPERTIES COMPILE_FLAGS "/arch:AVX2")
endif()