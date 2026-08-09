# KVMDongle

**KVMDongle** is a DIY KVM/crash-cart: an HDMI-capture+VGA dongle handles video (separate,
out of scope here), while a Raspberry Pi Zero W plugged into the target
machine's USB port emulates a keyboard, mouse, and a read-only CD-ROM
(backed by an ISO already on the Pi's SD card) -- enough to remote-control
a machine and install an OS on it. Your laptop talks to the Pi over a
USB-TTL serial adapter wired to the Pi's GPIO UART, running the app in
[client.py](client.py).

`old/` contains a previous, Arduino-based generation of this project, kept
for reference -- its UI is what `client.py` is modeled on.

## Hardware

- Raspberry Pi Zero W
- A blank/fresh [DietPi](https://dietpi.com/) SD card -- used instead of
  Raspberry Pi OS because the Zero W's single ARM11 core boots it
  noticeably faster (DietPi runs far fewer services by default). Both are
  Debian + systemd underneath, so everything in `pi/` works on either;
  `install.sh` doesn't care which one it's running on.
- A USB-to-TTL serial adapter, wired **to the Pi's GPIO pins** (not its
  USB port):
  - Adapter GND -> Pi GND
  - Adapter TX -> Pi GPIO15 (RXD)
  - Adapter RX -> Pi GPIO14 (TXD)
- The Pi's **USB** (OTG-capable) micro-USB port -> the target machine
- The Pi's **PWR IN** micro-USB port -> a wall adapter or powered hub

> **Power the Pi from PWR IN, not parasitically off the target.** The Pi
> Zero W has two micro-USB ports; only one ("USB") does data. If you power
> the Pi from the target's port instead of a dedicated supply, a brownout
> from a Wi-Fi burst or SD card write can drop keyboard+mouse+storage all
> at once, mid-session, with no way to recover without physically touching
> the target.

## Setting up the Pi (once, from a blank SD card)

1. Download the **DietPi image for "RPi 1/Zero (ARMv6)"** from
   [dietpi.com/downloads](https://dietpi.com/#downloadinfo) -- the Zero W's
   BCM2835 is ARMv6, a different image than the one used for Pi 2/3/4/Zero 2.
   Flash it (Raspberry Pi Imager's "Use custom" / balenaEtcher both work).
2. Before first boot, edit `dietpi.txt` on the flashed boot partition to set
   your Wi-Fi SSID/password and enable SSH -- the file is self-documented
   with comments for each option. Boot the Pi.
3. Log in over SSH (default `root` / `dietpi`; DietPi forces a password
   change on first login) and copy this repo to the Pi (`git clone` on the
   Pi, or `scp -r` from your laptop) so it has both `protocol.py` and the
   `pi/` directory.
4. On the Pi:
   ```
   cd KVMDongle/pi
   sudo ./install.sh
   sudo reboot
   ```
5. After reboot, confirm it came up:
   ```
   systemctl status kvmdongle-gadget kvmdongle-daemon
   ls /dev/hidg0 /dev/hidg1
   ```
6. Copy some `.iso` files into `/srv/kvmdongle/isos/` (SD card swap, `scp`,
   or the phase-2 Wi-Fi upload page below).

`install.sh` is idempotent -- safe to re-run if something goes wrong.

### What `install.sh` does

- Adds `dtoverlay=dwc2,dr_mode=peripheral` and `dtoverlay=disable-bt` and
  `enable_uart=1` to `config.txt`. `disable-bt` moves the Pi's better UART
  (PL011) off Bluetooth duty and onto the GPIO pins -- the alternative
  mini-UART's clock scales with CPU frequency and isn't reliable for this
  link.
- Strips any serial console from `cmdline.txt` and masks the getty units,
  so nothing fights the daemon for the port.
- Installs `python3-serial` via `apt` (not `pip` -- recent Debian-based
  releases block global `pip install`).
- Copies `protocol.py`, `pi/daemon.py`, and the gadget scripts to
  `/opt/kvmdongle/`, and enables the two systemd services
  (`kvmdongle-gadget`, `kvmdongle-daemon`).
- Reduces routine SD-card writes: remounts root `noatime` on every boot,
  makes the systemd journal volatile (RAM-only, capped at 20MB, lost on
  reboot), and disables swap if present. This is a lightweight measure,
  not a read-only root -- it doesn't protect against corruption from a
  write that's genuinely in progress when power is lost, just cuts down
  how often that risk window opens. A true read-only root is possible on
  DietPi (there's no built-in toggle like Raspberry Pi OS's, so it'd mean
  `overlayroot` plus a second SD-card partition for `/srv/kvmdongle/isos`,
  since the Zero W has no spare USB port for external writable storage) --
  ask if you want to move to that later.

## Running the laptop client

```
pip install -r requirements.txt
python client.py
```
Or run it as a real standalone application with no Python install needed
at all -- see [packaging/](packaging/) for building a Windows `.exe`,
macOS `.app`, or Linux `.run`.

Both `--serial-port` and `--capture-index` are optional -- omit either (or
both, as above) to auto-select the first one found, e.g. for running this
as a standalone, no-arguments application. Pass them explicitly only if
you need to pick a specific one out of several:
```
python client.py --serial-port COM5 --capture-index 1
```

- `--serial-port`: the USB-TTL adapter's port (Windows: `COM5`-style;
  Linux/Mac: `/dev/ttyUSB0`-style) -- omit to auto-select the first
  detected serial port
- `--capture-index`: your HDMI-capture device's OpenCV index (omit to
  auto-scan)
- `--capture-width` / `--capture-height` (default `1920`x`1080`) /
  `--capture-fourcc`: without an explicit width/height, some capture cards
  silently negotiate a lower-detail mode despite still reporting the same
  nominal frame size, which looks grainy/blurry -- requesting an explicit
  mode fixes it. Pass `0` for width/height to not request a specific one.
  See Troubleshooting if 1920x1080 isn't your capture card's native mode.
- `--baud`: must match `pi/daemon.py` (default `115200` on both ends -- a
  higher rate (460800) was tried and reverted; see Troubleshooting)
- `--debug`: prints every key/mouse event and Pi reply to the terminal

On Windows, `client.py` opens the capture device via DirectShow rather than
OpenCV's default Media Foundation backend, and marks the process
DPI-aware -- both fix issues that otherwise show up as "looks fine in OBS,
grainy/blurry here" (see Troubleshooting).

### Controls

- The window is resizable (drag an edge/corner, maximize, etc.). The video
  always keeps its own aspect ratio and letterboxes/pillarboxes into
  whatever space is left -- it's never stretched to fill the window.
- A small light in the top-right of the menu bar shows the link status to
  the Pi: green if it's replied to anything (including the periodic
  keepalive) in the last 5 seconds, red otherwise -- including at
  startup, before the first reply has arrived.
- Click into the video window, then type normally -- keystrokes go to the
  target whenever the window has focus.
- The mouse works like a touchscreen, not a captured relative pointer:
  click or drag anywhere in the video area to move the target's cursor to
  that position and click/drag there. Left/middle/right buttons all work.
  Scrolling forwards the wheel at the target's current cursor position.
  Your real cursor is never hidden or grabbed -- it's always free to also
  use the menu bar, since clicks are routed by whether they land above or
  below the menu bar strip, not by any mode switch.
- **Clipboard > Paste Clipboard** types clipboard text onto the target,
  character by character. **Macros** has Ctrl+Alt+Del, Alt+Tab, Alt+F4,
  Win+R, and Win+D. Both are menu-only -- no local hotkeys -- so every
  keystroke while the window is focused is always forwarded straight to
  the target, with nothing intercepted first.
- **Video** menu: switch capture devices without restarting `client.py` --
  lists detected indices (the active one marked with `*`) and has a
  Refresh item to re-scan, e.g. after plugging in another capture card.
  The active device isn't reopened just to confirm it's still there, so
  switching or refreshing never disrupts an in-progress capture. Shows a
  real device name alongside the index where it can get one (e.g.
  "Device 1 - Logitech C920") -- OpenCV has no portable way to ask for
  this, so it's best-effort and OS-specific: free on Linux (read from
  sysfs), needs `pip install pygrabber` on Windows (already in
  `requirements.txt`, Windows-only), not currently implemented on macOS.
  Falls back to a plain "Device N" wherever a name isn't available.
- **Storage** menu: lists ISOs on the Pi's SD card (queried live from the
  Pi -- it's the source of truth), mount one (exposed to the target as a
  read-only CD-ROM within a few seconds), or eject. The currently mounted
  one is marked with `*`.
- **Network** menu: enable/disable the Pi's ISO-upload Wi-Fi AP (phase 2
  below) remotely, instead of SSHing in to run `wifi-ap-toggle.sh`
  yourself. Only works if `install-webui.sh` has been run on the Pi --
  otherwise it replies with an error saying so. Switching it on/off takes
  a few seconds (hostapd/dnsmasq restarting) and briefly delays key/mouse
  forwarding while it runs, same as an ISO mount/eject.
- **Serial Port** menu: switch which serial port `client.py` talks to
  without restarting it -- lists detected ports (the active one marked
  with `*`) and has a Refresh item to re-scan, e.g. after plugging in a
  different USB-TTL adapter. The new port is only switched over once it's
  confirmed to open successfully; the old one is left untouched (and only
  closed after the switch) if it doesn't. Shows a real description
  alongside the port path where the OS has one (e.g. "COM5 - USB-SERIAL
  CH340 (COM5)") -- unlike capture devices, this is fully portable via
  `pyserial` itself on Windows/Linux/macOS, no extra dependency or
  per-OS code needed. Falls back to the bare port path if the OS doesn't
  have a description for it.
- **Debug** menu ("Open Pi Shell"): opens a real `bash` shell running
  on the Pi, over the same serial link, in its **own separate window** --
  not an overlay on the video, so it has its own taskbar entry and can be
  moved/resized/closed independently, and never steals keyboard/mouse
  focus from the main KVM window or vice versa. Interactive programs,
  colors, and cursor movement all work, so it looks and feels like an SSH
  session even though it's carried entirely over the KB/mouse/storage
  serial cable (no network involved). Press **F12** or just close the
  window to end the session; if the Pi's shell exits on its own (e.g. you
  typed `exit`), the window shows that and waits for you to close it,
  same as a real terminal emulator noticing its process ended. Requires
  the `pyte` package (see `requirements.txt`) -- if it's missing, clicking
  "Open Pi Shell" just prints a note to the terminal instead of opening
  anything.
- **Debug > Open Debug Log** opens another separate window showing
  everything `client.py` itself prints -- serial errors, macro/paste
  activity, Pi replies with `--debug`, and so on. This is the only way to
  see any of that once packaged as a windowed/console-less standalone
  application (a Windows EXE built without a console, a macOS `.app`
  bundle, ...), since there's no terminal for the OS to even attach in
  the first place -- every `print()` in the whole app would otherwise go
  nowhere anyone could ever see, errors included. Opening it late still
  shows everything printed since startup, not just from that point on,
  and lines mentioning "error"/"warning" are color-highlighted so
  problems jump out while scanning a long log.
- The **Debug** menu also has three power commands for the Pi itself,
  each of which shells out to `systemctl` on the Pi:
  - **Restart Daemon** -- one click, restarts just `kvmdongle-daemon`
    (e.g. if it's gotten into a stuck state). Reconnects on its own within
    a couple seconds; keyboard/mouse/storage briefly stop responding
    while it does.
  - **Reboot Pi** / **Shutdown Pi** -- click once to arm (the label
    changes to "...(click again to confirm)" and the dropdown stays open
    so the same item is right there under your cursor), then click it
    again within ~4 seconds to actually send it; otherwise it just
    disarms itself. Arming one disarms the other. A Pi Zero W has no
    remote power button, so **Shutdown Pi** means someone has to
    physically unplug/replug its power to bring it back -- there's no
    undo.
  
  None of these three get a "success" reply -- whatever would send one
  (the daemon process, or the whole machine) is exactly what's about to
  go away, so watch the connection-status light instead: it goes red
  once the Pi stops responding, same as any other disconnect.
- **Session > Quit** or the window's close button to exit.

Expect **1-5 seconds** after mounting/ejecting before the target's OS
actually notices the disc change -- that's the target polling for
removable media, not something the Pi/client can speed up.

## Adding more ISOs later (phase 2: Wi-Fi upload)

The Pi Zero W's Wi-Fi radio can host a private AP with a small upload
page, so you don't need to pull the SD card each time. Since the Zero W
has only one radio, **AP mode and normal Wi-Fi networking aren't
concurrent** -- treat this as something you switch on only while
uploading, not an always-on mode. `wifi-ap-toggle.sh` detects whether
wlan0 is normally managed by NetworkManager, dhcpcd, or plain ifupdown
(DietPi's default) and restores whichever one it found when you turn the
AP back off.

Set up once:
```
cd KVMDongle/pi
sudo ./install-webui.sh
# then edit /opt/kvmdongle/hostapd.conf: change ssid and wpa_passphrase
```

Use it:
```
sudo /opt/kvmdongle/wifi-ap-toggle.sh on    # join the SSID from hostapd.conf,
                                             # browse to http://192.168.50.1:8080
sudo /opt/kvmdongle/wifi-ap-toggle.sh off   # back to normal Wi-Fi
```

## Troubleshooting

- **No `/dev/hidg0`/`/dev/hidg1` after reboot**: `systemctl status
  kvmdongle-gadget` -- check `journalctl -u kvmdongle-gadget` for the
  failure. Common cause: `dtoverlay=dwc2,dr_mode=peripheral` didn't take
  (wrong `config.txt`/`config.txt` path, or the OTG data port wasn't used).
- **Target doesn't see anything at all**: confirm you're using the Pi's
  **USB** port (not PWR IN) for the target cable, and that it's powered
  separately from PWR IN.
- **Serial port opens but nothing happens**: check `journalctl -u
  kvmdongle-daemon`; a getty may still be holding the UART if
  `install.sh`'s console-stripping didn't match your image's `cmdline.txt`
  format exactly -- check for a lingering `console=serial0`/`ttyAMA0`/
  `ttyS0` entry there.
- **Mount takes a while / target doesn't see the new disc**: expected up
  to ~5s; if it never shows up, check `journalctl -u kvmdongle-daemon` for
  an `ERROR` reply (e.g. filename typo) -- the client's Storage menu also
  surfaces the error text.
- **Can't catch the target's BIOS/UEFI entry key (e.g. F1) by mashing it
  right after a reboot, even though everything works fine once the OS
  boots**: confirmed in practice -- if the Pi is powered parasitically
  from the target (against the PWR IN advice above), the target's own
  reboot cuts the Pi's power too, so the whole KVM dongle reboots right
  alongside it and obviously can't forward anything until it's booted
  back up, which takes far longer than the BIOS-entry window stays open.
  It can look like a software timing/reconnect issue since a serial
  session might still appear "open" right up until the moment power cuts,
  but the fix is purely physical: give the Pi its own dedicated,
  always-on power source, independent of the target machine's USB ports.
- **Video looks grainy/compressed at higher resolutions, but fine in
  OBS**: two separate things can cause this, and `client.py` already
  addresses both by default, but if it's still off:
  - *Windows DPI scaling*: apps that don't declare DPI-awareness get
    bitmap-stretched by Windows to match your display's scale factor
    (125%/150%/etc, common on laptop screens), which blurs everything --
    OBS is DPI-aware and never gets this treatment. `client.py` sets the
    `SDL_WINDOWS_DPI_AWARENESS` environment variable before `pygame.init()`
    so SDL registers awareness through its own mechanism; if it's somehow
    not taking effect, check your display scaling under Windows Settings >
    Display. (Don't call Windows' `SetProcessDpiAwareness()` directly
    instead -- that was tried first and caused mouse clicks to land in the
    wrong place, see below.)
  - *Capture format negotiation*: without an explicit width/height
    request, some capture cards silently fall back to a lower-detail mode
    at open time despite still reporting the same nominal frame size --
    confirmed on at least one cheap capture card, where requesting
    1920x1080 explicitly fixed it even though the device was already
    "reporting" 1920x1080 by default. `client.py` now requests
    1920x1080 by default for exactly this reason; if your capture card's
    native mode is something else, override with `--capture-width`/
    `--capture-height` (and `--capture-fourcc` if needed, e.g. `MJPG`).
- **Mouse movement feels laggy**: this was a real bug, fixed in two
  places -- `pi/daemon.py` used to open/close `/dev/hidg1` on every single
  HID report (expensive on a USB gadget character device, and mouse
  movement generates far more reports than keyboard input), and
  `client.py` used to call the capture device's blocking read directly in
  the same loop that polls keyboard/mouse input, so any capture hiccup
  delayed input forwarding too. Both are now decoupled (persistent file
  descriptor on the Pi, background capture thread on the laptop). If
  lag is still noticeable, check whether it's specifically video-capture
  hiccups by watching `--debug` output timestamps against actual mouse
  movement.
- **After upgrading, mouse clicks land in the wrong place / do nothing**:
  the mouse changed from a relative captured pointer to an absolute
  touchscreen-style one, which needed a different HID report shape on the
  gadget's mouse function (`pi/gadget-setup.sh`). A gadget that's already
  bound with the old shape won't pick up the new one on its own --
  re-run `sudo ./install.sh` on the Pi and reboot.
- **Every click lands in the same spot (often the bottom-right corner,
  toggling minimize/restore-all on Windows targets)**: this was a real bug
  -- an earlier build called Windows' `SetProcessDpiAwareness()` directly
  for the DPI-scaling fix above, which desynced SDL's idea of the window's
  coordinate space from Windows' actual (scaled) coordinates, so every
  mouse event's position came out scaled relative to the window's real
  pixel size -- clamped by `map_click_to_target()` into the video rect's
  corner every time. Fixed by using the `SDL_WINDOWS_DPI_AWARENESS`
  environment variable instead, which lets SDL handle DPI awareness
  through its own internal, self-consistent path.
- **After a while idle (e.g. waiting for the target to reboot/POST),
  keys/clicks stop reaching the target -- terminal shows repeated
  `[serial error] WriteFile failed ... 'The device does not recognize the
  command.'`, fixed by restarting `client.py`**: this was a real bug --
  Windows can put an idle USB-serial adapter to sleep (USB selective
  suspend), and the first read/write after it wakes can fail with a stale
  handle error that never recovered on its own; the reader thread used to
  die permanently on the first such error too, silently dropping every Pi
  reply from then on. Both read and write paths now reopen the port and
  retry automatically, and `client.py` sends a lightweight keepalive
  (reusing the existing `PING`/`PONG` frames) whenever the link's been
  idle for 2+ seconds, so Windows shouldn't consider the port idle enough
  to suspend in the first place. If it still happens, you can also disable
  USB selective suspend for the adapter directly: Device Manager -> Ports
  (COM & LPT) -> your adapter -> Properties -> Power Management -> untick
  "Allow the computer to turn off this device to save power" (also check
  this on the USB Root Hub it's plugged into, since suspension can happen
  at the hub level too).
- **Clicking any Storage/Network menu item (or sometimes just opening a
  dropdown) permanently freezes keyboard/mouse -- a client restart doesn't
  fix it, only restarting `kvmdongle-daemon` on the Pi does**: this was a
  real bug in `pi/daemon.py`. The daemon is single-threaded, and the AP
  enable/disable/status-check commands used to run inline, shelling out
  to `wifi-ap-toggle.sh`/`systemctl` with a nominal timeout -- but
  `subprocess.run(timeout=...)` only kills the *direct* child on timeout,
  not any grandchild it spawned (`nmcli`, `hostapd`, ...); if one of those
  was left holding the captured stdout/stderr pipes open, the read
  blocked forever regardless of the timeout, freezing the daemon's entire
  read-parse-handle loop -- keyboard and mouse included -- until it was
  restarted. (Once the daemon is actually wedged like that, every
  *subsequent* click looks like it "causes" the freeze too, since nothing
  is being processed anymore -- the real trigger is specifically an AP
  status check or enable/disable.) Fixed two ways: AP commands now run on
  a background thread, so they can never block keyboard/mouse no matter
  how long they take or whether they hang outright; and
  `WifiApController.set_enabled()` now runs the toggle script in its own
  process group and kills the *whole group* on timeout, so a hang gets
  cleaned up instead of just having its effects contained.
- **Mouse input freezes the whole session (keyboard included) specifically
  while the target is showing BIOS/UEFI, but is fine once it's booted into
  the OS**: this was a real bug in `pi/daemon.py`. HID report writes to
  `/dev/hidg*` went straight to a blocking file descriptor; `write()` to a
  USB HID gadget character device can block if the host isn't promptly
  polling/consuming that endpoint, and a BIOS/UEFI's minimal USB stack is
  much more likely to do that than a full OS. Since the daemon is
  single-threaded, one stalled mouse write froze the entire read-parse-
  handle loop, keyboard included, until the daemon was restarted. Fixed
  by opening the `/dev/hidg*` file descriptors `O_NONBLOCK`, so a write
  that can't complete immediately raises instead of blocking -- the
  existing error handling already just logs and drops that one report.
- **Client crashes outright with `Fatal Python error: pygame_parachute:
  Segmentation Fault`, usually during a burst of serial errors (e.g.
  right after a target reboot)**: this was a real, serious bug --
  `client.py`'s serial reconnect logic (added to fix the USB-suspend
  issue above) only locked its own close-and-reopen sequence, not the
  actual read/write calls. That left a real race: the background reader
  thread could be mid-`read()` on the port at the exact moment the write
  path closed it out from under it while reopening. On Windows, pyserial's
  overlapped-I/O structures get torn down by `close()`, and touching them
  from a read that was still in flight on another thread corrupted memory
  badly enough to crash the whole process, not just raise a catchable
  exception. Fixed by guarding every single touch of the serial handle --
  reads, writes, and reopens alike -- with one lock, and by never holding
  that lock through a blocking read (the read loop only ever reads bytes
  it already confirmed are waiting, sleeping outside the lock otherwise),
  so a reopen is never stuck waiting behind a long read either.
- **Opening a Storage/Network dropdown and clicking an item sends that
  click to the target machine instead of activating the item**: this was
  a real bug introduced by the touchscreen mouse redesign. `client.py`
  used to route every click purely by `y < MENU_HEIGHT` (top strip vs.
  video area), but an *open* dropdown's items are drawn below that strip
  -- so clicking one fell into "video area" and got forwarded to the
  target instead of ever reaching the menu. Fixed by trying the menu bar
  first for left-clicks (it knows its own actual extent, dropdown
  included) and only forwarding to the target if it says the click wasn't
  its.
- **A plain click moves the target's cursor to the right spot but doesn't
  actually click -- drag works fine, and clicking somewhere new is more
  reliable than clicking where the cursor already is**: this was a real,
  deeper bug than it first looked. Position and buttons used to travel as
  two separate frames/writes (an absolute-position report, immediately
  followed by a distinct button-down report) -- but a real HID absolute
  pointer always reports a complete position+button snapshot in a single
  sample, never splits them across two independently-timed reports. A
  short retry-with-backoff on the HID write (still in place, and still
  useful as defense-in-depth against a genuinely busy endpoint) helped but
  didn't fully fix it, and the "same position is less reliable" pattern
  pointed at the real issue: clicking without moving sends a position
  report identical to the one already in effect, immediately followed by
  a button-only report -- and something in that gap (host-side duplicate-
  report handling, endpoint queue timing, or both) made the pair
  unreliable specifically when nothing about the position changed.
  Dragging was unaffected since every motion sample already changes the
  position. Properly fixed by combining position and buttons into one
  `MOUSE_STATE` frame/report sent atomically -- see `protocol.py`'s
  `MOUSE_STATE` comment for the full explanation. This needs the same
  `sudo ./install.sh` + restart-the-daemon step as the mouse-descriptor
  change earlier, since it's a protocol change between `client.py` and
  `pi/daemon.py`.
- **Debug menu says "Open Pi Shell" but nothing happens, or the shell
  window opens but the Pi never replies**: the Terminal feature added new
  frame types to `protocol.py` (`SHELL_OPEN`/`SHELL_INPUT`/`SHELL_OUTPUT`/
  etc.) that a Pi still running an older `pi/daemon.py` doesn't know about
  -- same as any other protocol change, this needs `sudo ./install.sh` +
  a daemon restart on the Pi to pick up. Separately, if the `pyte`
  package isn't installed in the laptop's Python environment, clicking
  "Open Pi Shell" just prints a note to the terminal (`pyte is not
  installed -- run: pip install pyte`) instead of opening anything --
  `pip install pyte` (or `pip install -r requirements.txt`) fixes that;
  `client.py` spawns a second process (`terminal_window.py`) for the shell
  window itself (pygame can only own one window per process), so that
  process needs the same venv/interpreter as `client.py` -- run both from
  the same `pip install -r requirements.txt` environment.
- **Restart Daemon / Reboot Pi / Shutdown Pi menu items do nothing**: if
  `pi/daemon.py` is older than these frame types (`RESTART_DAEMON`/
  `REBOOT_PI`/`SHUTDOWN_PI`), it silently drops them -- run `sudo
  ./install.sh` on the Pi (and reconnect, for a reboot/shutdown, since
  there's nothing to restart into if the Pi itself is still on the old
  version) and check `journalctl -u kvmdongle-daemon` right after clicking
  one: a current daemon logs `invoking: systemctl ...` for every attempt,
  and a `WARNING: unknown frame type` if it's genuinely out of date --
  either tells you which case you're in. If it's not a version issue: this
  was also a real bug in `client.py` for **Reboot Pi**/**Shutdown Pi**
  specifically (not Restart Daemon, which is a single click) -- `MenuBar`
  closes its dropdown after every item click, so arming the action (the
  first click) immediately hid the very item you needed to click again to
  confirm, unless you noticed the label change and explicitly reopened
  the menu. Fixed by letting an item's handler return a truthy value to
  keep the dropdown open specifically on the arming click, so the second,
  confirming click can land on the same spot right away.
- **Mouse doesn't work on Linux target machines (keyboard is fine), and/or
  keyboard+mouse randomly stop working with `dmesg` showing repeating
  `usb ... reset high-speed USB device ... using xhci_hcd` and `device
  descriptor read/64, error -110`, sometimes forever (only a full
  replug/reboot recovers it) -- confirmed on an HP EliteDesk, a Lenovo
  ThinkCentre, and a Dell PowerEdge R530, never seen on Windows targets**:
  this was a real, serious bug (actually two symptoms of the same cause),
  and cable/power-supply/USB-hub swaps do NOT fix it. Several wrong
  theories were ruled out along the way (USB autosuspend, an empty
  mass-storage LUN, high-speed signal margin) before finding the actual
  cause: the mouse's HID report descriptor in `gadget-setup.sh` was
  exactly 64 bytes -- precisely `wMaxPacketSize0` for a high-speed control
  endpoint. A descriptor length that's an exact multiple of the control
  endpoint's max packet size needs a zero-length packet to terminate its
  `GET_DESCRIPTOR` transfer correctly, and some combination of the Pi's
  `dwc2` peripheral controller and the host's `xhci_hcd` mishandles that
  specific case -- 100% reproducibly, regardless of which other functions
  were present or the mouse's interface number (the keyboard's descriptor,
  63 bytes, never a multiple of 64, was never affected). Worse, it didn't
  just break the mouse (`usbhid: can't add hid device: -110`) -- it left
  the shared control endpoint in a bad enough state to also break mass
  storage's own later control requests (e.g. `GET_MAX_LUN`), which is why
  removing either the mouse *or* mass storage alone made the other work
  fine, and why an empty-vs-populated LUN made no difference (a red
  herring -- the LUN's contents were never the problem). Fixed by padding
  the mouse's report descriptor by one byte (widening `Usage Maximum(5)`
  from its 1-byte encoding to the equivalent, functionally identical
  2-byte encoding) so it's 65 bytes instead of 64 -- see the comment
  directly above the mouse's `report_desc` in `gadget-setup.sh` for the
  full writeup. Needs the usual `sudo ./install.sh` (or manually
  redeploying `gadget-setup.sh` to `/opt/kvmdongle/`) + `sudo systemctl
  restart kvmdongle-gadget` to pick up on an already-set-up Pi.
- **The connection-status light goes red during heavy, continuous
  keyboard/mouse use, even though everything is clearly still working**:
  this was a real bug in `client.py`. `is_connected()` only reflects the
  last time something was *received* from the Pi, but ordinary
  `KEY_DOWN`/`KEY_UP`/`MOUSE_STATE`/`MOUSE_SCROLL` frames never get a
  reply -- only a periodic keepalive `PING` (answered with a `PONG`) keeps
  that clock fresh during otherwise-quiet stretches. That keepalive used
  to be gated solely on how long it had been since the last *write* of
  any kind -- which sounds reasonable, but continuous keyboard/mouse
  activity is itself nonstop one-way write traffic, so that clock never
  went idle and the ping -- the only thing that could ever solicit a
  reply -- never fired. The indicator would flip red under exactly the
  busiest, most obviously-still-connected traffic pattern. Fixed by
  splitting it into two independent checks in `send_keepalive_if_idle()`:
  the original write-idle check (still there, for genuinely idle periods,
  to stop Windows USB selective suspend from kicking in) plus a second
  check gated on time since *we last sent a ping* specifically, which
  fires regardless of how much other traffic is flowing, guaranteeing a
  reply-soliciting ping at least every `KEEPALIVE_INTERVAL_SECONDS`
  no matter what.
- **`client.py` and `pi/daemon.py` can't communicate at all -- the
  connection light never turns green, no menu ever gets a reply**: check
  that `--baud` actually matches `pi/daemon.py`'s `BAUD_RATE` before
  anything else -- if they don't, every byte on the wire is misread
  relative to its actual bit timing, so the checksummed frame parser on
  both ends never resolves anything into a valid frame: total, silent
  communication failure, not a partial or intermittent one. This project
  actually tried raising the default from 115200 to 460800 at one point
  (after `disable-bt` frees up the Pi's good PL011 UART, which can
  comfortably run faster than the mini-UART could) and then reverted it
  back to 115200 -- confirmed directly with `screen` on a USB-TTL
  adapter/Mac combination that received nothing at all at 460800 but
  worked fine at 115200 (and 9600). The PL011 UART itself isn't the
  limit; the USB-serial adapter chip and its driver on the *laptop* end
  need to reliably support whatever rate is chosen too, and not every
  cheap adapter's driver does so cleanly at nonstandard high rates on
  every OS. 115200 is the safer universal default -- keyboard/mouse/
  control traffic is tiny regardless of baud, and even the Pi-shell
  terminal's live output is still comfortably interactive at 115200, just
  not as fast as 460800 would be for something like a giant `cat`. If you
  know your specific adapter/OS combination handles a higher rate
  reliably (confirm with `screen`/`minicom` directly, the same way this
  was diagnosed), raising `BAUD_RATE` in `pi/daemon.py` and `--baud`'s
  default in `client.py` together is fine -- just verify with a raw
  terminal first, not just by trying the app and guessing.
- **The Pi shell window shows "[session closed]" (the Pi's bash exited on
  its own) but then vanishes on its own ~1 second later instead of
  waiting for you to close it**: this was a real bug in `client.py`.
  `TerminalWindow.notify_closed()` used to call the exact same
  wait-then-terminate logic as `close()` (the user explicitly asking to
  end the session), which meant the window got forcibly killed shortly
  after showing the "session closed" banner -- directly contradicting the
  documented point of that banner (let the user notice and close it in
  their own time, like a real terminal emulator). Fixed by having
  `notify_closed()` just relay the notice and let the child process keep
  running independently until the user closes it themselves; only the
  user-initiated `close()` path still forcibly ends the window.
