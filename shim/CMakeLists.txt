cmake_minimum_required(VERSION 3.16)

cmake_policy(VERSION 3.15)
if (POLICY CMP0076)
    cmake_policy(SET CMP0076 NEW)
endif()

set(CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/../melonds-src/cmake" ${CMAKE_MODULE_PATH})

project(melonds_shim LANGUAGES C CXX ASM)

include(CheckSymbolExists)
include(CheckLibraryExists)
include(CMakeDependentOption)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ── Architecture detection (required by melonDS core) ──

function(detect_architecture symbol arch)
    if (NOT DEFINED ARCHITECTURE)
        set(CMAKE_REQUIRED_QUIET 1)
        check_symbol_exists("${symbol}" "" ARCHITECTURE_${arch})
        unset(CMAKE_REQUIRED_QUIET)
        if (ARCHITECTURE_${arch})
            set(ARCHITECTURE "${arch}" PARENT_SCOPE)
            set(ARCHITECTURE_${arch} 1 PARENT_SCOPE)
            add_definitions(-DARCHITECTURE_${arch}=1)
        endif()
    endif()
endfunction()

detect_architecture("__x86_64__" x86_64)
detect_architecture("__i386__" x86)
detect_architecture("__arm__" ARM)
detect_architecture("__aarch64__" ARM64)

# ── Options for melonDS core ──

cmake_dependent_option(ENABLE_JIT "Enable JIT recompiler" ON
    "ARCHITECTURE STREQUAL x86_64 OR ARCHITECTURE STREQUAL ARM64" OFF)
option(ENABLE_OGLRENDERER "Enable OpenGL renderer" OFF)
option(ENABLE_GDBSTUB "Enable GDB stub" OFF)

if (ENABLE_GDBSTUB)
    add_definitions(-DGDBSTUB_ENABLED)
endif()

# ── Build melonDS core as a static library ──

add_subdirectory(
    "${CMAKE_CURRENT_SOURCE_DIR}/../melonds-src/src"
    "${CMAKE_CURRENT_BINARY_DIR}/core_build"
)

# ── Build our shim as a shared library ──

add_library(melonds SHARED
    melonds_shim.cpp
    platform_stubs.cpp
)

target_include_directories(melonds PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../melonds-src/src"
    "${CMAKE_CURRENT_BINARY_DIR}/core_build"  # for version.h
)

target_link_libraries(melonds PRIVATE core dl)

# Strip the lib prefix so the file is just "melonds.so" -- no, keep "libmelonds.so" for convention
set_target_properties(melonds PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
)
