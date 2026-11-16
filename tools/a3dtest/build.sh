#!/usr/bin/env bash
# Build a3dtest.exe (and loadtest.exe) for Windows 98 with the MinGW cross compiler,
# without a C runtime.
# Needs the A3D 2.0 SDK header: set SDK_INC to the folder holding ia3dapi.h
#   (git clone https://github.com/sigmaco/a3d-sdk; SDK_INC=a3d-sdk/a3d-sdk-v2.0/sdk/inc)
# loadtest.exe and a3d3test.exe need the A3D 3.0 header instead (SDK3_INC); it is skipped if
# that folder is missing.
set -e
SDK_INC=${SDK_INC:-./a3d-sdk/a3d-sdk-v2.0/sdk/inc}
SDK3_INC=${SDK3_INC:-$(dirname "$SDK_INC")/../../a3d-sdk-v3.0/sdk/inc}
CC=${CC:-i686-w64-mingw32-gcc}
$CC -std=gnu99 -O2 -march=i586 -mfpmath=387 -ffreestanding -fno-builtin -fno-stack-protector \
    -fno-asynchronous-unwind-tables -Wall -Wno-unused-function -I"$SDK_INC" \
    -nostdlib -Wl,--entry,_entry -Wl,--subsystem,windows:4.0 \
    -Wl,--major-os-version,4 -Wl,--minor-os-version,0 \
    -Wl,--major-subsystem-version,4 -Wl,--minor-subsystem-version,0 \
    -Wl,--disable-dynamicbase -Wl,--disable-nxcompat -Wl,--disable-reloc-section \
    -o a3dtest.exe a3dtest.c -lkernel32 -luser32 -lole32 -luuid -lwinmm -lgcc
echo built a3dtest.exe
if [ -f "$SDK3_INC/ia3dapi.h" ]; then
    $CC -std=gnu99 -O2 -march=i586 -mfpmath=387 -ffreestanding -fno-builtin -fno-stack-protector \
        -fno-asynchronous-unwind-tables -Wall -I"$SDK3_INC" \
        -nostdlib -Wl,--entry,_entry -Wl,--subsystem,windows:4.0 \
        -Wl,--major-os-version,4 -Wl,--minor-os-version,0 \
        -Wl,--major-subsystem-version,4 -Wl,--minor-subsystem-version,0 \
        -Wl,--disable-dynamicbase -Wl,--disable-nxcompat -Wl,--disable-reloc-section \
        -o loadtest.exe loadtest.c -lkernel32 -luser32 -lole32 -luuid -lgcc
    echo built loadtest.exe
    $CC -std=gnu99 -O2 -march=i586 -mfpmath=387 -ffreestanding -fno-builtin -fno-stack-protector \
        -fno-asynchronous-unwind-tables -Wall -I"$SDK3_INC" \
        -nostdlib -Wl,--entry,_entry -Wl,--subsystem,windows:4.0 \
        -Wl,--major-os-version,4 -Wl,--minor-os-version,0 \
        -Wl,--major-subsystem-version,4 -Wl,--minor-subsystem-version,0 \
        -Wl,--disable-dynamicbase -Wl,--disable-nxcompat -Wl,--disable-reloc-section \
        -o a3d3test.exe a3d3test.c -lkernel32 -luser32 -lole32 -luuid -lgcc
    echo built a3d3test.exe
fi
