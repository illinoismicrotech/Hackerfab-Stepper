#!/usr/bin/env bash
# Build a project-local Tcl/Tk with Xft for standalone Python's bitmap-only Tk.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$PROJECT_DIR/.runtime/tk-build"
PREFIX="$PROJECT_DIR/.runtime/tk-9.0.4"
mkdir -p "$BUILD_DIR"
command -v gcc >/dev/null
command -v make >/dev/null
pkg-config --exists xft x11 fontconfig
fetch() {
    local component="$1" checksum="$2"
    if [[ ! -f "$BUILD_DIR/$component.tar.gz" ]]; then
        curl --max-time 90 -fL "https://github.com/tcltk/$component/archive/refs/tags/core-9-0-4.tar.gz" -o "$BUILD_DIR/$component.tar.gz"
    fi
    echo "$checksum  $BUILD_DIR/$component.tar.gz" | sha256sum --check
    tar -xzf "$BUILD_DIR/$component.tar.gz" -C "$BUILD_DIR"
}
fetch tcl 8c01bac92a9a8ce271b63d3aa28f09cde2c6762d6719ca671cdc8aee25391234
fetch tk ab9bd89fcff2e1248b7a9c097035f817fa0725285028c95686fdb92db65bc148
cd "$BUILD_DIR/tcl-core-9-0-4/unix"
./configure --prefix="$PREFIX" --enable-shared --enable-threads --without-system-libtommath LDFLAGS="-Wl,-soname,libtcl9.0.so"
make clean
make -j4
make install
cd "$BUILD_DIR/tk-core-9-0-4/unix"
./configure --prefix="$PREFIX" --with-tcl="$PREFIX/lib" --enable-xft --enable-shared LDFLAGS="-Wl,-soname,libtcl9tk9.0.so"
make clean
make -j4
make install
printf '\nBuilt font-enabled Tk in %s\n' "$PREFIX"
