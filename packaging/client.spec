# -*- mode: python ; coding: utf-8 -*-
"""
client.spec

PyInstaller build spec for the KVMDongle laptop client -- produces a
real, standalone desktop application (no separate Python install needed
to run it) on whichever OS you build it on.

Built as --onedir (a folder containing the exe/app + its dependencies),
DELIBERATELY not --onefile: client.py spawns a SECOND COPY OF ITSELF as a
subprocess every time you open the Pi Shell or Debug Log window (see
_child_process_argv() in client.py). A --onefile build re-extracts its
entire bundled payload from scratch on every single launch, which would
make opening either of those windows noticeably slow every time. --onedir
pays that extraction cost once, at build time, not on every relaunch.

PyInstaller cannot cross-compile -- this must be run ON the OS you're
building for. Usage (from the repo root):
    pip install pyinstaller
    pyinstaller packaging/client.spec

Output:
    dist/KVMDongle/                 (Windows, Linux -- a folder, run the
                                      .exe or the extensionless binary
                                      inside it)
    dist/KVMDongle.app/             (macOS)
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all

APP_NAME = "KVMDongle"

# SPECPATH is provided by PyInstaller at spec-exec time -- resolving
# relative to it (not the current working directory) means `pyinstaller
# packaging/client.spec` works the same regardless of where it's invoked
# from.
REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821

# OpenCV loads some of its own backend/plugin modules dynamically rather
# than through ordinary top-level imports PyInstaller's static analysis
# can see -- collect_all() is the standard, documented fix for "works
# when run from source, ImportError/DLL-not-found once frozen" that's
# specific to opencv-python.
cv2_datas, cv2_binaries, cv2_hidden = collect_all("cv2")

a = Analysis(  # noqa: F821
    [os.path.join(REPO_ROOT, "client.py")],
    pathex=[REPO_ROOT],
    binaries=cv2_binaries,
    datas=cv2_datas,
    # client.py's own dispatch (bottom of the file) only imports these
    # inside a runtime-conditional branch, triggered by a hidden CLI flag
    # from _child_process_argv() rather than an unconditional top-level
    # import -- PyInstaller's static analysis normally still finds plain
    # `import` statements regardless of what branch they're textually in,
    # but they're listed explicitly here too as a cheap safety net
    # against ever silently dropping them from the build, which would
    # quietly break the Terminal/Debug Log windows only once packaged.
    hiddenimports=["terminal_window", "debug_log_window", "protocol"] + cv2_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir: binaries go through COLLECT below, not into the exe itself
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed, no console -- that's what the Debug Log window is for
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier="com.kvmdongle.app",
        info_plist={
            "NSCameraUsageDescription": "Needed to read the HDMI capture device as a video source.",
            "NSHighResolutionCapable": True,
        },
    )
