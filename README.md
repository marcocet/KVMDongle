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

## Running the laptop client

```
pip install -r requirements.txt
python client.py --serial-port COM5 --capture-index 1
```

- `--serial-port`: the USB-TTL adapter's port (Windows: `COM5`-style;
  Linux/Mac: `/dev/ttyUSB0`-style)
- `--capture-index`: your HDMI-capture device's OpenCV index (omit to
  auto-scan)
- `--baud`: must match `pi/daemon.py` (default `460800` on both ends)
- `--debug`: prints every key/mouse event and Pi reply to the terminal

### Controls

- Click into the video window, then type normally -- keystrokes go to the
  target whenever the window has focus.
- **F9** toggles mouse capture (relative mouse input to the target).
  While off, your real cursor is free to use the menu bar.
- **F11** pastes clipboard text onto the target, character by character.
- **Ctrl+Shift+F1..F5**: Ctrl+Alt+Del, Alt+Tab, Alt+F4, Win+R, Win+D.
- **Storage** menu: lists ISOs on the Pi's SD card (queried live from the
  Pi -- it's the source of truth), mount one (exposed to the target as a
  read-only CD-ROM within a few seconds), or eject. The currently mounted
  one is marked with `*`.
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
