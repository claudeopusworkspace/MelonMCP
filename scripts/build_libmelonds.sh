#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/build"

echo "=== Building libmelonds.so ==="
echo "Project root: $PROJECT_ROOT"
echo "Build dir:    $BUILD_DIR"

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

cmake "$PROJECT_ROOT/shim" \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_JIT=ON \
    -DENABLE_OGLRENDERER=OFF \
    -DENABLE_GDBSTUB=OFF

cmake --build . -j"$(nproc)"

echo ""
echo "=== Build complete ==="
echo "Library: $BUILD_DIR/libmelonds.so"
ls -lh "$BUILD_DIR/libmelonds.so"
