# usb_udev_namer

A tool that gives USB serial devices (`ttyUSB*`, `ttyACM*`) a fixed name, such as `/dev/usb_imu`.

Linux assigns `/dev/ttyUSB0`, `/dev/ttyUSB1` to USB serial devices by plug-in order, so they can swap whenever you
change ports or re-plug. The proper fix is a udev rule that creates a symlink; this tool writes the rule, applies it
and verifies it on the spot through an interactive menu, so you do not need to know udev syntax:

Plug in the device → pick it from the list → type a name → the screen shows `/dev/<name> -> ttyUSB0`

## Requirements

- Linux or WSL2 (macOS has no udev, so this does not apply; use `/dev/cu.usbserial*` directly)
- Python 3.8 or later (standard library only, no `pip install` needed)
- `sudo` permission (needed to write to `/etc/udev/rules.d/`)
- WSL2: the device must first be attached to WSL with [wsl_usb_auto](https://github.com/alianlbj23/wsl_usb_auto),
  otherwise this tool cannot see it

## Installation

Put `usb_udev_namer.py` anywhere you like; nothing else needs to be installed.

## Usage

### Interactive menu (simplest)

Run it without arguments and the menu appears:

```bash
python3 usb_udev_namer.py
```

```
========== USB device fixed names (udev) ==========
Names: 1 (usb_imu -> ttyUSB0)
Shortcut: usbname
  1. Name a device
  2. Show all names (with health check)
  3. Delete a name
  4. Re-apply the rules (use when a name is missing after re-plugging)
  5. Show the USB serial devices currently plugged in
  6. Set a shortcut command (then type usbname to open this menu)
  7. Remove the shortcut command
  8. Remove everything (the rules file and all settings)
  0. Quit
Choose [0-8]:
```

The flow of "1. Name a device":

```
USB serial devices currently plugged in:
  1. /dev/ttyUSB0  10c4:ea60  Silicon Labs CP2102 USB to UART Bridge Controller  serial 0001  port 1-3.2
Choose a device [1-1] (0 to go back): 1
Enter a name (it will appear under /dev/, e.g. usb_imu; press Enter to cancel): usb_imu

Identified by: VID:PID 10c4:ea60 + serial 0001. Any USB port works.
Identify by physical USB port instead? (you must rename after moving to another port) [y/N]
This will write /etc/udev/rules.d/99-usb-udev-namer.rules (requires sudo). Continue? [y/N] y
[sudo] password for user:
Applying...
lrwxrwxrwx 1 root root 7 Nov 11 10:00 /dev/usb_imu -> ttyUSB0
```

"Set a shortcut command" in the menu adds an alias to `.bashrc` (`usbname` by default); after that, typing `usbname`
opens the menu.

### Command mode (for scripts; the menu can do all of it, no need to memorise)

| Command | Description |
|---------|-------------|
| `python3 usb_udev_namer.py scan` | List the USB serial devices currently plugged in |
| `python3 usb_udev_namer.py add /dev/ttyUSB0 -n usb_imu` | Name a device; add `--by auto\|serial\|port` to force the identification method |
| `python3 usb_udev_namer.py list` | List all names with the health check |
| `python3 usb_udev_namer.py remove usb_imu` | Delete a name |
| `python3 usb_udev_namer.py apply` | Regenerate the rules from the records and apply them |
| `python3 usb_udev_namer.py uninstall` | Delete the rules file, all settings and the `.bashrc` block (asks twice) |

`add` exits with code 1 when the name does not appear, so scripts can detect it.

## How devices are identified

A computer has only three pieces of information to tell two USB devices apart: VID:PID, serial number and physical
USB port. The tool chooses automatically:

| Situation | Identified by | In practice |
|-----------|---------------|-------------|
| It has a serial, and "VID:PID + serial" is unique among the devices currently plugged in | VID:PID + serial | Any port works |
| It has no serial, or is identical to another plugged-in device | Physical USB port + VID:PID | Always use the same port; rename after moving to another port |

So **when you have two or more devices of the same model, plug all of them in while naming**; only then can the tool
see that their serials are identical and switch to the physical port automatically.
"VID:PID only" is not offered: a later device with the same chip would silently collide with the name.

## How it works

1. Names are recorded in `~/.config/usb-udev-namer/rules.json` (the single source of truth).
2. After every change, `/etc/udev/rules.d/99-usb-udev-namer.rules` is regenerated in full, for example:

   ```
   SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", ATTRS{serial}=="0001", SYMLINK+="usb_imu", MODE="0666"
   SUBSYSTEM=="tty", KERNELS=="1-3.4", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", SYMLINK+="usb_esp32", MODE="0666"
   ```

3. Applying: `sudo cp` → `sudo udevadm control --reload-rules` → `sudo udevadm trigger --subsystem-match=tty --action=add`,
   then it waits up to 3 seconds for `/dev/<name>` to appear and prints `ls -l`. If sudo or udevadm fails,
   `rules.json` is not updated.
4. `MODE="0666"` lets every user read and write, which avoids joining the dialout group and logging in again; to
   tighten permissions, change the `MODE` constant in the script.
5. The tool only touches the one rules file it generates, never the other files in `/etc/udev/rules.d/`.

## Related files

| Path | Purpose |
|------|---------|
| `~/.config/usb-udev-namer/rules.json` | Name records |
| `/etc/udev/rules.d/99-usb-udev-namer.rules` | udev rules generated by the tool (do not edit by hand) |
| `~/.bashrc` | Shortcut alias (wrapped in `# >>> usb-udev-namer >>>` markers, so `uninstall` can remove it cleanly) |

## Troubleshooting

- **The name does not appear**: choose "4. Re-apply the rules". If it still does not, choose "2. Show all names" and
  read the health check.
- **The name never appears on WSL**: WSL may not start the udev service by default; run `sudo service udev restart`
  and choose 4 again. The permanent fix is to add this to `/etc/wsl.conf`:

  ```ini
  [boot]
  systemd=true
  ```

  then run `wsl --shutdown` on Windows to restart WSL.
- **The health check says "matches 2 devices"**: the two devices have identical VID:PID and serial (only one was
  plugged in while naming). Choose 3 to delete that name, plug both devices in and name it again; the tool will switch
  to the physical port automatically.
- **The name disappears after moving to another USB port**: that name is identified by physical port; plug the device
  back into the original port, or name it again.
- **sudo authentication failed**: wrong password, or the account has no sudo permission; nothing is changed in this
  case.

## Tests

```bash
python3 -m pytest tests -q
python3 -m flake8 usb_udev_namer.py tests
```

The tests need neither root nor real devices: sysfs is simulated with a temporary directory, and external commands
are replaced with fake objects.

## License

Apache-2.0
