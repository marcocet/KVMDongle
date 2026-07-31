# KVM / Crash-Cart Dongle

A DIY KVM/crash-cart: an HDMI-capture+VGA dongle handles video (separate,
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
python client.py --serial-port COM5 --capture-index 1
```

- `--serial-port`: the USB-TTL adapter's port (Windows: `COM5`-style;
  Linux/Mac: `/dev/ttyUSB0`-style)
- `--capture-index`: your HDMI-capture device's OpenCV index (omit to
  auto-scan)
- `--capture-width` / `--capture-height` (default `1920`x`1080`) /
  `--capture-fourcc`: without an explicit width/height, some capture cards
  silently negotiate a lower-detail mode despite still reporting the same
  nominal frame size, which looks grainy/blurry -- requesting an explicit
  mode fixes it. Pass `0` for width/height to not request a specific one.
  See Troubleshooting if 1920x1080 isn't your capture card's native mode.
- `--baud`: must match `pi/daemon.py` (default `460800` on both ends)
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
- **F11** pastes clipboard text onto the target, character by character.
- **Ctrl+Shift+F1..F5**: Ctrl+Alt+Del, Alt+Tab, Alt+F4, Win+R, Win+D.
- **Video** menu: switch capture devices without restarting `client.py` --
  lists detected indices (the active one marked with `*`) and has a
  Refresh item to re-scan, e.g. after plugging in another capture card.
  The active device isn't reopened just to confirm it's still there, so
  switching or refreshing never disrupts an in-progress capture.
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
  closed after the switch) if it doesn't.
- **Terminal** menu ("Open Pi Shell" / **F12** to close): opens a
  full-screen terminal overlay running a real `bash` shell on the Pi over
  the same serial link -- interactive programs, colors, and cursor
  movement all work, so it looks and feels like an SSH session even
  though it's carried entirely over the KB/mouse/storage serial cable
  (no network involved). While it's open, keyboard and mouse input goes
  to the shell instead of the target machine; press **F12** at any time
  to close it and resume normal KVM control. Requires the `pyte` package
  (see `requirements.txt`) -- the menu item says so if it's missing.
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
- **Terminal menu says "Open Pi Shell" but nothing happens, or the Pi
  never replies once it's open**: the Terminal feature added new frame
  types to `protocol.py` (`SHELL_OPEN`/`SHELL_INPUT`/`SHELL_OUTPUT`/etc.)
  that a Pi still running an older `pi/daemon.py` doesn't know about --
  same as any other protocol change, this needs `sudo ./install.sh` +
  a daemon restart on the Pi to pick up. Separately, the menu item itself
  says "(install pyte)" instead of "Open Pi Shell" if the `pyte` package
  isn't installed in the laptop's Python environment -- `pip install
  pyte` (or `pip install -r requirements.txt`) fixes that.
