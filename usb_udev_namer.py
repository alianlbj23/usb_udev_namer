#!/usr/bin/env python3
"""
USB device fixed-name tool (udev symlink generator).

Give a USB serial device (ttyUSB*, ttyACM*) a fixed name such as /dev/usb_imu,
so the name always points to the same device no matter the plug-in order or
how many times it is re-plugged.

Simplest usage: just run it and a menu appears
  python3 usb_udev_namer.py

Advanced (command mode, for scripts; the menu can do all of it, no need to memorise):
  python3 usb_udev_namer.py scan                              list the USB serial devices currently plugged in
  python3 usb_udev_namer.py add /dev/ttyUSB0 -n usb_imu       name a device [--by auto|serial|port]
  python3 usb_udev_namer.py list                              list all names (with health check)
  python3 usb_udev_namer.py remove usb_imu                    delete a name
  python3 usb_udev_namer.py apply                             re-apply the rules
  python3 usb_udev_namer.py uninstall                         remove the rules file and all settings

Names are recorded in ~/.config/usb-udev-namer/rules.json, and
/etc/udev/rules.d/99-usb-udev-namer.rules is regenerated in full after every change.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

USB_TTY_RE = re.compile(r"^tty(USB|ACM)(\d+)\Z")
# udev's == is a glob match, so a serial may only contain safe characters (none of * ? [ ] " \)
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:/+-]{1,126}\Z")
SYSFS_TEXT_LIMIT = 256


# ---------- Helpers ----------
class UserError(Exception):
    """An error message meant for the user."""


@dataclass
class Result:
    """The result of an external command."""

    returncode: int
    stdout: str
    stderr: str


# ---------- sysfs ----------
@dataclass(frozen=True)
class Device:
    """A USB serial device that is currently plugged in."""

    tty: str
    vid: str
    pid: str
    serial: str
    product: str
    manufacturer: str
    port: str


def _read_attr(directory, name):
    """Read a sysfs attribute file; return an empty string if it is missing or unreadable."""
    try:
        text = (directory / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text.strip()[:SYSFS_TEXT_LIMIT]


def _usb_device_dir(device_link, sysfs_root):
    """Walk up from the tty's device link; the first directory with idVendor is the USB device itself."""
    if not device_link.exists():
        return None
    root = Path(os.path.realpath(sysfs_root))
    current = Path(os.path.realpath(device_link))
    while current != root and root in current.parents:
        if (current / "idVendor").is_file():
            return current
        current = current.parent
    return None


def _tty_sort_key(name):
    m = USB_TTY_RE.match(name)
    return (m.group(1), int(m.group(2)))


def scan_devices(sysfs_root):
    """List every USB serial device under sysfs."""
    tty_class = Path(sysfs_root) / "class" / "tty"
    if not tty_class.is_dir():
        return []
    devices = []
    for name in sorted((n for n in os.listdir(tty_class) if USB_TTY_RE.match(n)), key=_tty_sort_key):
        usb = _usb_device_dir(tty_class / name / "device", sysfs_root)
        if usb is None:
            continue
        serial = _read_attr(usb, "serial")
        vid, pid = _read_attr(usb, "idVendor").lower(), _read_attr(usb, "idProduct").lower()
        if not (HEX4_RE.match(vid) and HEX4_RE.match(pid) and PORT_RE.match(usb.name)):
            continue        # malformed values must not be written into a rule
        devices.append(Device(
            tty=name,
            vid=vid,
            pid=pid,
            serial=serial if SERIAL_RE.match(serial) else "",
            product=_read_attr(usb, "product"),
            manufacturer=_read_attr(usb, "manufacturer"),
            port=usb.name,
        ))
    return devices


def device_label(dev):
    """Return the text shown for a device in the menu."""
    desc = " ".join(s for s in (dev.manufacturer, dev.product) if s)
    serial = f"serial {dev.serial}" if dev.serial else "no serial"
    parts = [f"/dev/{dev.tty}", f"{dev.vid}:{dev.pid}"] + ([desc] if desc else []) + [serial, f"port {dev.port}"]
    return "  ".join(parts)


# ---------- Identification strategy ----------
BY_CHOICES = ("auto", "serial", "port")
PORT_HINT = "               Always plug it into the same port; rename it after moving to another port."


@dataclass
class Choice:
    """The matching method decided at naming time. twin is another device with identical VID:PID + serial."""

    dev: Device
    rule: dict
    twin: object = None
    forced: bool = False

    def explain(self):
        """Return the user-facing explanation of how the device is identified."""
        d, ids = self.dev, f"{self.dev.vid}:{self.dev.pid}"
        if self.rule["by"] == "serial":
            text = f"Identified by: VID:PID {ids} + serial {d.serial}. Any USB port works."
            if self.twin is not None:
                text += (f"\nWarning: /dev/{self.twin.tty} has the same VID:PID and serial, so this name matches "
                         "both devices and it is impossible to tell which one it points to. "
                         "Identifying by physical port is recommended.")
            return text
        if self.forced:
            head = f"Identified by: physical USB port {d.port} (VID:PID {ids})."
        elif self.twin is not None:
            head = (f"Identified by: same VID:PID and serial as /dev/{self.twin.tty}, "
                    f"so physical USB port {d.port} is used instead.")
        else:
            head = f"Identified by: this device has no serial number, so physical USB port {d.port} is used instead."
        return head + "\n" + PORT_HINT


def _same_identity(a, b):
    return (a.vid, a.pid, a.serial) == (b.vid, b.pid, b.serial)


def choose_match(dev, devices, by="auto"):
    """Decide per the section 4 strategy whether dev is identified by serial or by physical port.

    devices is every device currently plugged in.
    """
    if by not in BY_CHOICES:
        raise UserError(f"The identification method must be one of {', '.join(BY_CHOICES)}; got: {by}")
    siblings = [o.tty for o in devices if o.tty != dev.tty and o.port == dev.port]
    if siblings:
        ttys = ", ".join([dev.tty] + siblings)
        raise UserError(f"The USB device behind /dev/{dev.tty} exposes multiple serial ports ({ttys}); "
                        "a rule would match all of them and cannot point to just one. "
                        "Naming such devices is not supported in this version")
    twin = next((o for o in devices if o.tty != dev.tty and _same_identity(o, dev)), None) if dev.serial else None
    base = {"vid": dev.vid, "pid": dev.pid}
    if by == "serial" and not dev.serial:
        raise UserError(f"/dev/{dev.tty} has no serial number; "
                        "it can only be identified by physical port (--by port or auto)")
    if by == "serial" or (by == "auto" and dev.serial and twin is None):
        return Choice(dev, dict(base, by="serial", serial=dev.serial), twin, forced=(by == "serial"))
    return Choice(dev, dict(base, by="port", port=dev.port), twin, forced=(by == "port"))


def matches(rule, dev):
    """Return whether rule (an entry in rules.json) matches dev; same logic as the generated udev rule."""
    if (rule["vid"], rule["pid"]) != (dev.vid, dev.pid):
        return False
    if rule["by"] == "serial":
        return rule["serial"] == dev.serial
    return rule["port"] == dev.port


# ---------- Rule text ----------
# Teaching environments favour "plug in and it works", skipping the dialout group and re-login;
# change this to tighten permissions
MODE = "0666"
RULES_OWNER = "usb_udev_namer.py"
RULES_HEADER = (
    f"# Generated by {RULES_OWNER}. Do not edit by hand.\n"
    "# Names come from ~/.config/usb-udev-namer/rules.json; "
    "to add or delete a name, run python3 usb_udev_namer.py\n"
)


def rule_line(name, rule):
    """Return the udev rule for one name."""
    ids = f'ATTRS{{idVendor}}=="{rule["vid"]}", ATTRS{{idProduct}}=="{rule["pid"]}"'
    if rule["by"] == "serial":
        match = f'{ids}, ATTRS{{serial}}=="{rule["serial"]}"'
    else:
        match = f'KERNELS=="{rule["port"]}", {ids}'
    return f'SUBSYSTEM=="tty", {match}, SYMLINK+="{name}", MODE="{MODE}"'


def render_rules(rules):
    """Return the content of the whole udev rules file."""
    return RULES_HEADER + "".join(f"\n{rule_line(n, rules[n])}" for n in sorted(rules)) + ("\n" if rules else "")


# ---------- rules.json ----------
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}\Z")
HEX4_RE = re.compile(r"^[0-9a-f]{4}\Z")
PORT_RE = re.compile(r"^\d+-\d+(\.\d+)*\Z")
# Device names the kernel creates itself; invisible in /dev while unplugged,
# but they clash with the symlink once the device is plugged in
RESERVED_NAME_RE = re.compile(r"^(tty(S|USB|ACM|AMA|THS|GS)?\d*|console|ptmx|pts|null|zero|full|random|urandom|mem|kmsg"
                              r"|sd[a-z]+\d*|hd[a-z]+\d*|nvme\S*|mmcblk\S*|loop\d*|video\d+|media\d+|hidraw\d+"
                              r"|input|serial|disk|bus|usb|char|block|shm|mqueue)\Z")


def rules_file(env):
    """Return the path of rules.json."""
    return env.config_dir / "rules.json"


def _rule_problem(name, rule):
    """Check one rule and return a description of the problem, or None if it is fine."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return f"invalid name: {name!r}"
    if not isinstance(rule, dict):
        return f"the value of {name} is not an object"
    if not all(isinstance(rule.get(k), str) and HEX4_RE.match(rule[k]) for k in ("vid", "pid")):
        return f"vid/pid of {name} is not 4 lowercase hex digits"
    if rule.get("by") == "serial":
        ok = set(rule) == {"vid", "pid", "by", "serial"} and isinstance(rule["serial"], str) \
            and SERIAL_RE.match(rule["serial"])
    elif rule.get("by") == "port":
        ok = set(rule) == {"vid", "pid", "by", "port"} and isinstance(rule["port"], str) \
            and PORT_RE.match(rule["port"])
    else:
        return f"'by' of {name} must be serial or port"
    return None if ok else f"the {rule['by']} field of {name} is missing or malformed"


def load_rules(env):
    """Read rules.json (the single source of truth) and validate its content."""
    path = rules_file(env)
    if not path.exists():
        return {}
    hint = f"Fix {path}, or choose 'Remove everything' in the menu to start over"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UserError(f"Cannot read rules.json: {e}. {hint}") from e
    if not isinstance(data, dict):
        raise UserError(f"Malformed rules.json: the top level must be an object. {hint}")
    for name, rule in data.items():
        problem = _rule_problem(name, rule)
        if problem:
            raise UserError(f"Malformed rules.json: {problem}. {hint}")
    return data


def save_rules(env, rules):
    """Write a temporary file and rename it, so a power cut mid-write cannot leave a broken file."""
    env.config_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=env.config_dir, prefix=".rules.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, rules_file(env))
    finally:
        if os.path.exists(tmp):     # the temporary file is only still there on failure
            os.unlink(tmp)


def validate_name(name, rules, dev_root):
    """Check a name entered by the user; raise UserError if it is not acceptable."""
    if not NAME_RE.match(name):
        raise UserError(f"Invalid name '{name}': only letters, digits, underscore, dot and hyphen are allowed; "
                        "it must not start with a dot or hyphen and may be at most 32 characters")
    if name in rules:
        raise UserError(f"The name '{name}' is already taken; pick another one, or delete the old one first")
    if os.path.lexists(Path(dev_root) / name):
        raise UserError(f"/dev/{name} already exists (used by the system or another program); pick another name")
    if RESERVED_NAME_RE.match(name):
        raise UserError(f"'{name}' is a name used by system devices (it would clash once such a device "
                        "is plugged in); pick another one, e.g. usb_imu")
    return name


# ---------- Health check ----------
@dataclass
class HealthRow:
    """One row of the name table."""

    name: str
    how: str
    ok: bool
    status: str
    hint: str = ""


def describe_rule(rule):
    """Return a short description of how a rule identifies its device, e.g. "10c4:ea60 serial 0001"."""
    key = f"serial {rule['serial']}" if rule["by"] == "serial" else f"port {rule['port']}"
    return f"{rule['vid']}:{rule['pid']} {key}"


def _link_target(dev_root, name):
    try:
        return os.path.basename(os.readlink(Path(dev_root) / name))
    except OSError:
        return None


def health_check(rules, devices, dev_root):
    """Recompute the state of every name against the plugged-in devices, using the udev rules' matching logic."""
    rows = []
    for name in sorted(rules):
        rule = rules[name]
        hit = [d.tty for d in devices if matches(rule, d)]
        row = HealthRow(name, describe_rule(rule), False, "not plugged in")
        if len(hit) > 1:
            row.status = f"Warning: matches {len(hit)} devices ({', '.join(hit)}); cannot tell which one"
            row.hint = ("→ choose 3 to delete it, then name it again with both devices plugged in "
                        "so the physical port is used automatically" if rule["by"] == "serial"
                        else "→ this USB device exposes multiple serial ports (e.g. a dual-port chip); "
                             "this version cannot name them separately")
        elif hit:
            target = _link_target(dev_root, name)
            if target == hit[0]:
                row.ok, row.status = True, f"OK  /dev/{name} -> {hit[0]}"
            elif target is None:
                row.status = f"Warning: the device is plugged in ({hit[0]}), but /dev/{name} did not appear"
                row.hint = "→ choose 4 to re-apply the rules"
            else:
                row.status = f"Warning: /dev/{name} points to {target}, but the matching device is {hit[0]}"
                row.hint = "→ choose 4 to re-apply the rules"
        rows.append(row)
    return rows


def _width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text, width):
    return text + " " * (width - _width(text))


def format_health(rows):
    """Lay the health check results out as a table; wide characters count as two columns so columns line up."""
    w_name = max([_width("Name")] + [_width(r.name) for r in rows]) + 2
    w_how = max([_width("Identifier")] + [_width(r.how) for r in rows]) + 2
    lines = [_pad("Name", w_name) + _pad("Identifier", w_how) + "Status"]
    for r in rows:
        lines.append(_pad(r.name, w_name) + _pad(r.how, w_how) + r.status)
        if r.hint:
            lines.append(" " * (w_name + w_how) + r.hint)
    return "\n".join(lines)


# ---------- WSL ----------
def is_wsl(env):
    """Return whether this is WSL, i.e. /proc/version contains microsoft."""
    try:
        return "microsoft" in (env.proc / "version").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def udevd_running(env):
    """Scan /proc/<pid>/comm for the udev service."""
    try:
        pids = [p for p in os.listdir(env.proc) if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            comm = (env.proc / pid / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue        # the process just exited
        if comm in ("systemd-udevd", "udevd"):
            return True
    return False


def wsl_udev_warning(env):
    """Return the warning to show when udev is not running on WSL, or None if no warning is needed."""
    if not is_wsl(env) or udevd_running(env):
        return None
    return ("⚠ The udev service is not running in WSL, so names will not appear. "
            "Run this first: sudo service udev restart\n"
            "  Permanent fix: add systemd=true under the [boot] section of /etc/wsl.conf, "
            "then run wsl --shutdown on Windows to restart")


WSL_ATTACH_HINT = "WSL users: attach the device to WSL with wsl_usb_auto first, otherwise it is not visible here."


# ---------- External commands ----------
SUDO_AUTH_TIMEOUT = 120     # leave time for the user to type the password
CMD_TIMEOUT = 30
LINK_WAIT_SECONDS = 3.0
LINK_POLL_SECONDS = 0.1


def run_cmd(cmd, timeout, interactive=False):
    """Run an external command (list form, no shell, always with a timeout).

    With interactive=True the output is not captured, so sudo can ask for the password on the terminal.
    """
    try:
        if interactive:
            r = subprocess.run(cmd, timeout=timeout, check=False)
            return Result(r.returncode, "", "")
        r = subprocess.run(cmd, timeout=timeout, check=False, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, errors="replace")
    except FileNotFoundError as e:
        raise UserError(f"Command not found: {cmd[0]}. Make sure it is installed") from e
    except subprocess.TimeoutExpired as e:
        raise UserError(f"Command timed out ({timeout} s): {' '.join(cmd)}") from e
    return Result(r.returncode, r.stdout, r.stderr)


# ---------- Runtime environment (injectable, for testing) ----------
@dataclass
class Env:
    """Every path and function that touches the system; replaced with fakes in tests."""

    sysfs: Path = Path("/sys")
    dev: Path = Path("/dev")
    proc: Path = Path("/proc")
    config_dir: Path = Path.home() / ".config" / "usb-udev-namer"
    bashrc: Path = Path.home() / ".bashrc"
    udev_rules: Path = Path("/etc/udev/rules.d/99-usb-udev-namer.rules")
    run: object = run_cmd
    ask: object = input
    out: object = print
    sleep: object = time.sleep
    which: object = shutil.which
    is_root: bool = field(default_factory=lambda: os.geteuid() == 0)


class Quit(Exception):
    """The user pressed Ctrl+D (EOF); leave the menu."""


def ask(env, prompt):
    """Ask the user a question; turn EOF into Quit."""
    try:
        return env.ask(prompt).strip()
    except EOFError as e:
        raise Quit from e


def yes(env, prompt):
    """Ask a y/N question; the default is N."""
    return ask(env, prompt).lower() in ("y", "yes")


# ---------- Applying rules (needs root) ----------
def _root_cmd(env, cmd):
    return list(cmd) if env.is_root else ["sudo", "-n"] + list(cmd)


def _sudo_auth(env):
    if env.is_root:
        return
    if env.run(["sudo", "-v"], timeout=SUDO_AUTH_TIMEOUT, interactive=True).returncode != 0:
        raise UserError("sudo authentication failed (wrong password, or this account has no sudo permission); "
                        "nothing was changed.")


def _root_run(env, cmd, what):
    r = env.run(_root_cmd(env, cmd), timeout=CMD_TIMEOUT)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip() or f"exit code {r.returncode}"
        raise UserError(f"Failed to {what}: {detail}\n  The name records were not updated; "
                        "after fixing the problem, choose 4 in the menu to re-apply.")


def _reload_udev(env):
    _root_run(env, ["udevadm", "control", "--reload-rules"], "reload the udev rules")
    _root_run(env, ["udevadm", "trigger", "--subsystem-match=tty", "--action=add"], "trigger udev")


def _ensure_ours(env):
    """Refuse to touch an existing rules file unless this tool generated it.

    The file is ours when its first line is a comment naming this script,
    which also covers the headers written by earlier versions.
    """
    if not os.path.lexists(env.udev_rules):
        return
    try:
        with open(env.udev_rules, encoding="utf-8", errors="replace") as f:
            first = f.readline()
    except OSError as e:
        raise UserError(f"Cannot read {env.udev_rules}: {e}") from e
    if not (first.startswith("#") and RULES_OWNER in first):
        raise UserError(f"{env.udev_rules} already exists but was not generated by this tool; "
                        "to be safe it is neither overwritten nor deleted. "
                        "Check its content and deal with it yourself")


def apply_rules(env, rules):
    """Regenerate the whole udev rules file from rules and apply it.

    Raise UserError on failure; the caller must not update rules.json then.
    """
    _ensure_ours(env)
    _sudo_auth(env)
    env.out("Applying...")
    fd, tmp = tempfile.mkstemp(prefix="usb-udev-namer.", suffix=".rules")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(render_rules(rules))
        os.chmod(tmp, 0o644)
        _root_run(env, ["cp", tmp, str(env.udev_rules)], f"write {env.udev_rules}")
    finally:
        os.unlink(tmp)
    _reload_udev(env)


def wait_for_link(env, name, present=True):
    """Wait up to LINK_WAIT_SECONDS seconds for /dev/<name> to appear (or disappear)."""
    link = env.dev / name
    for _ in range(int(LINK_WAIT_SECONDS / LINK_POLL_SECONDS) + 1):
        if os.path.lexists(link) == present:
            return True
        env.sleep(LINK_POLL_SECONDS)
    return False


def report_link(env, name, tty):
    """Wait for the symlink, confirm it points to tty and print ls -l.

    If it does not appear, spell out the likely causes. Return whether it succeeded.
    """
    if wait_for_link(env, name):
        r = env.run(["ls", "-l", str(env.dev / name)], timeout=CMD_TIMEOUT)
        env.out((r.stdout or r.stderr).rstrip())
        target = _link_target(env.dev, name)
        if target == tty:
            return True
        env.out(f"✗ /dev/{name} points to {target}, not the {tty} you just selected. "
                "Choose 2 to see the health check")
        return False
    env.out(f"✗ The rules were written, but /dev/{name} did not appear within {LINK_WAIT_SECONDS:g} seconds. "
            "Possible causes:\n"
            "  - the udev service is not running (common on WSL) → run sudo service udev restart, "
            "then choose 4 to re-apply\n"
            "  - the device was unplugged during setup → plug it back in, then choose 4 to re-apply\n"
            "  - the rule does not match the device → choose 2 to see the health check")
    return False


# ---------- Actions (shared by the menu and command mode) ----------
def add_name(env, choice, name):
    """Save choice as name and return whether it succeeded.

    rules.json is written only after the rules were applied; the symlink is verified last.
    """
    rules = load_rules(env)
    validate_name(name, rules, env.dev)
    new = dict(rules)
    new[name] = choice.rule
    apply_rules(env, new)
    save_rules(env, new)
    return report_link(env, name, choice.dev.tty)


def remove_name(env, name):
    """Delete a name and re-apply."""
    rules = load_rules(env)
    if name not in rules:
        raise UserError(f"No name called '{name}' (use list to see them)")
    new = {k: v for k, v in rules.items() if k != name}
    apply_rules(env, new)
    save_rules(env, new)
    env.out(f"✓ Deleted name: {name}")
    if not wait_for_link(env, name, present=False):
        env.out(f"  /dev/{name} is still there; it will disappear once the device is unplugged and plugged back in")


def reapply(env):
    """Re-apply from rules.json, then print the health check."""
    rules = load_rules(env)
    apply_rules(env, rules)
    devices = scan_devices(env.sysfs)
    for name, rule in rules.items():
        if sum(matches(rule, d) for d in devices) == 1:
            wait_for_link(env, name)
    env.out("✓ Re-applied")
    show_names(env)


def show_names(env):
    """Print all names with the health check."""
    rules = load_rules(env)
    if not rules:
        env.out("There are no names yet. Choose 1 in the menu to name a device, "
                "or run: python3 usb_udev_namer.py add /dev/ttyUSB0 -n usb_imu")
        return
    env.out(format_health(health_check(rules, scan_devices(env.sysfs), env.dev)))


def show_devices(env, devices=None):
    """Print the USB serial devices currently plugged in and return the device list."""
    devices = scan_devices(env.sysfs) if devices is None else devices
    if not devices:
        env.out("No USB serial devices detected (ttyUSB*, ttyACM*). Make sure the device is plugged in.")
        if is_wsl(env):
            env.out(WSL_ATTACH_HINT)
        return devices
    rules = load_rules(env)
    for i, d in enumerate(devices, 1):
        named = [n for n in sorted(rules) if matches(rules[n], d)]
        env.out(f"  {i}. {device_label(d)}" + (f"  (named {', '.join(named)})" if named else ""))
    return devices


def find_device(env, arg, devices):
    """Map /dev/ttyUSB0, ttyUSB0 or /dev/serial/by-id/... to a plugged-in device."""
    tty = arg
    if arg.startswith("/dev/"):
        tty = os.path.basename(os.path.realpath(env.dev / arg[len("/dev/"):]))
    for d in devices:
        if d.tty == tty:
            return d
    names = ", ".join(f"/dev/{d.tty}" for d in devices) or "none"
    msg = f"Cannot find {arg}. USB serial devices currently plugged in: {names}"
    raise UserError(msg + ("\n" + WSL_ATTACH_HINT if is_wsl(env) else ""))


def uninstall(env):
    """Delete the rules file, reload, delete the config directory and remove the .bashrc block."""
    if os.path.lexists(env.udev_rules):
        _ensure_ours(env)
        _sudo_auth(env)
        _root_run(env, ["rm", "-f", str(env.udev_rules)], f"delete {env.udev_rules}")
        _reload_udev(env)
    if env.config_dir.exists():
        shutil.rmtree(env.config_dir)
    alias = get_alias(env)
    remove_alias(env)
    env.out("✓ Removed the rules file and all settings"
            + (f"; the shortcut command {alias} was removed too" if alias else ""))


def confirm_uninstall(env):
    """Ask twice before removing everything."""
    return (yes(env, f"Delete all names, {env.udev_rules} and the settings? [y/N] ")
            and yes(env, "Please confirm once more; this cannot be undone. Really remove everything? [y/N] "))


# ---------- Shortcut command (alias) ----------
SCRIPT = Path(__file__).resolve()
ALIAS_BEGIN = "# >>> usb-udev-namer >>>"
ALIAS_END = "# <<< usb-udev-namer <<<"
ALIAS_RE = re.compile(r"\n*" + re.escape(ALIAS_BEGIN) + r".*?" + re.escape(ALIAS_END) + r"\n?", re.S)
ALIAS_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\Z")
DEFAULT_ALIAS = "usbname"


def get_alias(env):
    """Return the shortcut command name currently set in .bashrc, or None if there is none."""
    if not env.bashrc.exists():
        return None
    m = ALIAS_RE.search(env.bashrc.read_text(encoding="utf-8", errors="surrogateescape"))
    n = re.search(r"^alias ([^=\s]+)=", m.group(0), re.M) if m else None
    return n.group(1) if n else None


def set_alias(env, name):
    """Write (or replace) the shortcut command block in .bashrc."""
    if not ALIAS_NAME_RE.match(name):
        raise UserError(f"'{name}' cannot be used as a command name: only letters, digits, underscore and hyphen "
                        "are allowed, and it must not start with a digit")
    text = env.bashrc.read_text(encoding="utf-8", errors="surrogateescape") if env.bashrc.exists() else ""
    text = ALIAS_RE.sub("\n", text).rstrip("\n")
    block = (f"{ALIAS_BEGIN}\n"
             f"alias {name}={shlex.quote('python3 ' + shlex.quote(str(SCRIPT)))}\n"
             f"{ALIAS_END}\n")
    env.bashrc.write_text((text + "\n\n" if text else "") + block, encoding="utf-8", errors="surrogateescape")


def remove_alias(env):
    """Remove the shortcut command block from .bashrc and return whether anything was removed."""
    if not env.bashrc.exists():
        return False
    text = env.bashrc.read_text(encoding="utf-8", errors="surrogateescape")
    new = ALIAS_RE.sub("\n", text)
    if new.strip() == "":
        new = ""
    elif new != text:
        new = new.rstrip("\n") + "\n"
    env.bashrc.write_text(new, encoding="utf-8", errors="surrogateescape")
    return new != text


# ---------- Interactive menu ----------
def _is_number(text):
    """Accept ASCII digits only (characters like a superscript two pass isdigit() but fail int())."""
    return text.isascii() and text.isdigit()


def pick(env, count, prompt):
    """Let the user pick 1..count, or 0 to go back (returns None); ask again on any other input."""
    while True:
        ans = ask(env, f"{prompt} [1-{count}] (0 to go back): ")
        if _is_number(ans) and 0 <= int(ans) <= count:
            return int(ans) - 1 if int(ans) else None
        env.out(f"Please enter a number from 0 to {count}")


def menu_add(env):
    """Option 1: name a device."""
    devices = scan_devices(env.sysfs)
    if not devices:
        show_devices(env, devices)      # explains that no device was found (plus the wsl_usb_auto reminder on WSL)
        return
    if is_wsl(env):
        env.out(WSL_ATTACH_HINT)
    env.out("USB serial devices currently plugged in:")
    show_devices(env, devices)
    idx = pick(env, len(devices), "Choose a device")
    if idx is None:
        return
    dev = devices[idx]
    rules = load_rules(env)
    while True:
        name = ask(env, "Enter a name (it will appear under /dev/, e.g. usb_imu; press Enter to cancel): ")
        if not name:
            return
        try:
            validate_name(name, rules, env.dev)
            break
        except UserError as e:
            env.out(f"✗ {e}")
    choice = choose_match(dev, devices)
    env.out("")
    env.out(choice.explain())
    if choice.rule["by"] == "serial" and yes(env, "Identify by physical USB port instead? "
                                                  "(you must rename after moving to another port) [y/N] "):
        choice = choose_match(dev, devices, by="port")
        env.out(choice.explain())
    if not yes(env, f"This will write {env.udev_rules} (requires sudo). Continue? [y/N] "):
        env.out("Cancelled; nothing was changed")
        return
    add_name(env, choice, name)


def menu_remove(env):
    """Option 3: delete a name."""
    rules = load_rules(env)
    if not rules:
        env.out("There are no names yet")
        return
    names = sorted(rules)
    for i, n in enumerate(names, 1):
        env.out(f"  {i}. {n}  ({describe_rule(rules[n])})")
    idx = pick(env, len(names), "Name to delete")
    if idx is not None and yes(env, f"Delete {names[idx]}? [y/N] "):
        remove_name(env, names[idx])


def menu_set_alias(env):
    """Option 6: set the shortcut command."""
    current = get_alias(env)
    if current:
        env.out(f"The current shortcut command is: {current}")
    name = ask(env, f"Which command should open this menu? (press Enter for '{DEFAULT_ALIAS}'): ") or DEFAULT_ALIAS
    existing = env.which(name)
    if name != current and existing:
        if not yes(env, f"The system already has a command called '{name}' ({existing}). Override it? [y/N] "):
            return
    set_alias(env, name)
    env.out(f"✓ Shortcut command set: {name}")
    env.out("  It works in any new terminal; to use it in this window right away, run: source ~/.bashrc")


def menu_remove_alias(env):
    """Option 7: remove the shortcut command."""
    current = get_alias(env)
    if not current:
        env.out("No shortcut command is set")
        return
    if yes(env, f"Remove the shortcut command '{current}'? [y/N] "):
        remove_alias(env)
        env.out(f"✓ Shortcut command removed: {current}")
        env.out(f"  New terminals will no longer have it; in this window, run unalias {current} to drop it right away")


def menu_uninstall(env):
    """Option 8: remove everything."""
    if confirm_uninstall(env):
        uninstall(env)


def _summary(env):
    rules = load_rules(env)
    if not rules:
        return "Names: 0"
    parts = []
    for row in health_check(rules, scan_devices(env.sysfs), env.dev):
        if row.ok:
            parts.append(row.status.split("/dev/", 1)[1])
        elif row.status == "not plugged in":
            parts.append(f"{row.name} not plugged in")
        else:
            parts.append(f"{row.name} ⚠")
    return f"Names: {len(rules)} ({', '.join(parts)})"


MENU = [
    ("Name a device", menu_add),
    ("Show all names (with health check)", show_names),
    ("Delete a name", menu_remove),
    ("Re-apply the rules (use when a name is missing after re-plugging)", reapply),
    ("Show the USB serial devices currently plugged in", show_devices),
    (f"Set a shortcut command (then type {DEFAULT_ALIAS} to open this menu)", menu_set_alias),
    ("Remove the shortcut command", menu_remove_alias),
    ("Remove everything (the rules file and all settings)", menu_uninstall),
]


def menu(env):
    """Run the interactive menu; end normally on EOF."""
    warning = wsl_udev_warning(env)
    if warning:
        env.out(warning)
    try:
        while True:
            env.out("\n========== USB device fixed names (udev) ==========")
            try:
                env.out(_summary(env))
            except UserError as e:
                env.out(f"✗ {e}")
            env.out(f"Shortcut: {get_alias(env) or 'not set'}")
            for i, (label, _) in enumerate(MENU, 1):
                env.out(f"  {i}. {label}")
            env.out("  0. Quit")
            ans = ask(env, f"Choose [0-{len(MENU)}]: ")
            if ans == "0":
                return
            if not (_is_number(ans) and 1 <= int(ans) <= len(MENU)):
                env.out("Please enter a number from the menu")
                continue
            env.out("")
            try:
                MENU[int(ans) - 1][1](env)
            except UserError as e:
                env.out(f"✗ {e}")
            ask(env, "\nPress Enter to return to the menu...")
    except Quit:
        env.out("")


# ---------- Command mode ----------
def cmd_add(env, a):
    """Command mode: add."""
    warning = wsl_udev_warning(env)
    if warning:
        env.out(warning)
    devices = scan_devices(env.sysfs)
    dev = find_device(env, a.device, devices)
    validate_name(a.name, load_rules(env), env.dev)
    choice = choose_match(dev, devices, by=a.by)
    env.out(choice.explain())
    return 0 if add_name(env, choice, a.name) else 1


def cmd_uninstall(env, a):
    """Command mode: uninstall (also asks twice)."""
    if confirm_uninstall(env):
        uninstall(env)
    else:
        env.out("Cancelled")
    return 0


def build_parser():
    """Define the command mode arguments."""
    ap = argparse.ArgumentParser(description="USB device fixed-name tool (run without arguments to open the menu)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("scan", help="list the USB serial devices currently plugged in")
    p = sub.add_parser("add", help="name a device")
    p.add_argument("device", help="device path, e.g. /dev/ttyUSB0")
    p.add_argument("-n", "--name", required=True, help="name, e.g. usb_imu")
    p.add_argument("--by", choices=BY_CHOICES, default="auto", help="identification method (default: auto)")
    sub.add_parser("list", help="list all names (with health check)")
    p = sub.add_parser("remove", help="delete a name")
    p.add_argument("name")
    sub.add_parser("apply", help="re-apply the rules")
    sub.add_parser("uninstall", help="remove the rules file and all settings")
    return ap


COMMANDS = {
    "scan": lambda env, a: show_devices(env) and 0,
    "add": cmd_add,
    "list": lambda env, a: show_names(env),
    "remove": lambda env, a: remove_name(env, a.name),
    "apply": lambda env, a: reapply(env),
    "uninstall": cmd_uninstall,
}


def main(argv=None, env=None):
    """Run the program and return the exit code."""
    a = build_parser().parse_args(argv)
    env = env or Env()
    try:
        if a.cmd is None:
            menu(env)
            return 0
        return COMMANDS[a.cmd](env, a) or 0
    except UserError as e:
        env.out(f"✗ {e}")
        return 1
    except Quit:
        return 1
    except KeyboardInterrupt:
        env.out("")
        return 0 if a.cmd is None else 130


if __name__ == "__main__":
    sys.exit(main())
