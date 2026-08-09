# Packaging the laptop client as a standalone app

Produces a real desktop application on each OS -- no separate Python
install needed to run it, just double-click. Built with
[PyInstaller](https://pyinstaller.org/), which **cannot cross-compile**:
each script must be run on the OS it's building for.

| Platform | Script                        | Output                                  | Tested here? |
|----------|-------------------------------|------------------------------------------|--------------|
| Windows  | `build_windows.ps1`           | `dist/KVMDongle.exe` (single file)       | Yes          |
| macOS    | `build_macos.sh`               | `dist/KVMDongle.app`                    | No (see below)|
| Linux    | `build_linux.sh`               | `dist/KVMDongle-Linux.run`               | No (see below)|

All three run `pyinstaller packaging/client.spec` under the hood; the
`.spec` file itself is one shared, cross-platform build definition (it
branches on `sys.platform` internally -- `--onefile` for Windows,
`--onedir` (+ `.app` bundling) for macOS/Linux, see below), so there's a
single place to change build configuration for all three OSes.

## Windows is `--onefile`; macOS/Linux stay `--onedir` -- why the split

`client.py` spawns a **second copy of itself** as a subprocess every time
you open the Pi Shell or Debug Log window (see `_child_process_argv()`
in `client.py`) -- pygame/SDL only supports one window per process, so a
real second window needs a real second process. A `--onefile` build has
to re-extract its entire bundled payload from scratch on *every single
launch*, **relaunches included** -- opening either of those windows will
be noticeably slower than with a `--onedir` build, on every click, not
just the first one.

That trade-off is accepted on **Windows** because a literal single `.exe`
was the actual ask. It's *not* needed on **macOS** (a `.app` bundle is
already a single double-clickable thing from Finder's perspective, even
though it's a folder underneath) or **Linux** (`build_linux.sh` already
wraps the `--onedir` output into one `.run` file via `makeself`) -- both
already deliver "one thing to double-click/run" without paying the
relaunch-latency cost, so there was nothing to trade off there.

## The frozen-relaunch mechanism (why Terminal/Debug Log still work once packaged)

Running from source, opening the Pi Shell or Debug Log window spawns
`[sys.executable, "terminal_window.py"]` (or `debug_log_window.py`) --
a real Python interpreter running the helper script directly.

Once frozen, `sys.executable` **is the packaged app itself**, not a
Python interpreter, and the helper `.py` files aren't separately
runnable files on disk anymore -- they're bundled inside the app. So a
frozen build instead relaunches **another copy of the same exe** with a
hidden flag (`--_internal=terminal_window` / `--_internal=debug_log_window`),
and the dispatch at the bottom of `client.py` (checked before `main()` or
argparse ever runs) routes that copy straight into the helper module's
own `main()` instead of the normal client.py flow. This is implemented
in `client.py` itself (`_child_process_argv()` + the `if __name__ ==
"__main__":` block), not in these packaging scripts -- nothing extra to
do here, just worth knowing it's there and why, since skipping it would
have silently broken both windows the moment this app got packaged.

## Common prerequisites (all platforms)

```
pip install -r requirements.txt
pip install pyinstaller
```
The build scripts do this for you already.

## Windows

```
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```
Built and smoke-tested in this environment as a true single-file
`KVMDongle.exe`: it launches, `--serial-port`/`--capture-index`
auto-detect and the usual startup messages all work identically to
running from source, and launching it directly with
`--_internal=debug_log_window` (what opening the Debug Log window does
internally) correctly opens that window rather than crashing on an
unrecognized argument -- confirming the frozen-relaunch dispatch (see
above) actually works under `--onefile`, not just `--onedir`.

## macOS

```
chmod +x packaging/build_macos.sh
./packaging/build_macos.sh
```
**Not independently tested** (no macOS environment available where this
was written) -- please report back if anything needs adjusting. Two
things to know going in:
- **Gatekeeper**: an unsigned/unnotarized `.app` gets a "cannot be opened
  because the developer cannot be verified" warning on first launch on
  someone else's Mac. Right-click > Open bypasses it once; real
  distribution to others would need an Apple Developer ID to codesign +
  notarize, which is out of scope here.
- The Video menu's device-name lookup isn't implemented on macOS yet
  (falls back to plain "Device N") -- unrelated to packaging, see the
  main README's Troubleshooting section.

## Linux

Needs `makeself` (a separate system tool, not a pip package) to wrap the
PyInstaller output into the requested single `.run` file:
```
sudo apt install makeself      # or see https://github.com/megastep/makeself
chmod +x packaging/build_linux.sh
./packaging/build_linux.sh
```
**Not independently tested** (no Linux environment available where this
was written) -- please report back if anything needs adjusting for your
distro. Running the result:
```
chmod +x dist/KVMDongle-Linux.run
./dist/KVMDongle-Linux.run
```

## After building, on any platform

The Debug Log window (`Debug > Open Debug Log`) is the main way to see
what's going on once there's no console attached at all -- worth opening
first if something doesn't seem to be working in a packaged build.
