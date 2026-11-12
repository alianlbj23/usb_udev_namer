import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import usb_udev_namer as m  # noqa: E402


def add_usb_tty(sysfs, tty, port, vid="10c4", pid="ea60", serial="0001",
                product="CP2102 USB to UART Bridge Controller", manufacturer="Silicon Labs"):
    """Create a USB serial device in the fake sysfs tree, laid out like the real /sys.

    ttyUSB: .../<port>/<port>:1.0/ttyUSBn/tty/ttyUSBn, device -> .../<port>:1.0/ttyUSBn
    ttyACM: .../<port>/<port>:1.0/tty/ttyACMn, device -> .../<port>:1.0
    """
    usb_dir = Path(sysfs) / "devices" / "pci0000:00" / "0000:00:14.0" / "usb1" / port
    intf = usb_dir / f"{port}:1.0"
    if tty.startswith("ttyUSB"):
        device = intf / tty
        class_dir = device / "tty" / tty
    else:
        device = intf
        class_dir = intf / "tty" / tty
    class_dir.mkdir(parents=True)
    # the interface directory has its own attributes, but no idVendor
    (intf / "bInterfaceClass").write_text("ff\n")
    for key, value in (("idVendor", vid), ("idProduct", pid), ("serial", serial),
                       ("product", product), ("manufacturer", manufacturer)):
        if value is not None:
            (usb_dir / key).write_text(value + "\n")
    os.symlink(device, class_dir / "device")
    tty_class = Path(sysfs) / "class" / "tty"
    tty_class.mkdir(parents=True, exist_ok=True)
    os.symlink(class_dir, tty_class / tty)


class FakeRunner:
    """Replace subprocess: record commands, return configured results, and optionally simulate udev symlinks."""

    def __init__(self, env):
        self.env = env
        self.calls = []
        self.fail = {}        # command keyword -> returncode
        self.links = {}       # /dev/<name> -> tty links to create after trigger

    def __call__(self, cmd, timeout, interactive=False):
        assert timeout > 0
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for key, code in self.fail.items():
            if key in joined:
                return m.Result(code, "", f"{key} failed")
        if "cp" in cmd:
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_text(Path(cmd[-2]).read_text())
        if "rm" in cmd:
            Path(cmd[-1]).unlink(missing_ok=True)
        if "trigger" in cmd:
            for name, tty in self.links.items():
                link = self.env.dev / name
                if not os.path.lexists(link):
                    os.symlink(tty, link)
        if cmd[0] == "ls":
            return m.Result(0, f"lrwxrwxrwx 1 root root 7 Nov 11 10:00 {cmd[-1]} -> "
                               f"{os.readlink(cmd[-1])}\n", "")
        return m.Result(0, "", "")


class Console:
    """Inject input/print: raise EOFError once answers run out, simulating the user pressing Ctrl+D."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.lines = []

    def ask(self, prompt):
        self.lines.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def out(self, *args):
        self.lines.append(" ".join(str(a) for a in args))

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def env(tmp_path):
    sysfs = tmp_path / "sys"
    (sysfs / "class" / "tty").mkdir(parents=True)
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "null").touch()
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "version").write_text("Linux version 6.8.0-generic\n")
    console = Console()
    e = m.Env(
        sysfs=sysfs, dev=dev, proc=proc,
        config_dir=tmp_path / "config",
        bashrc=tmp_path / "bashrc",
        udev_rules=tmp_path / "rules.d" / "99-usb-udev-namer.rules",
        ask=console.ask, out=console.out,
        sleep=lambda s: None, which=lambda name: None, is_root=False,
    )
    e.run = FakeRunner(e)
    e.console = console
    return e
