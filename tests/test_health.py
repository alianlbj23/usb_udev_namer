import os
import unicodedata

import usb_udev_namer as m

IMU = {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"}
ESP = {"vid": "10c4", "pid": "ea60", "by": "port", "port": "1-3.4"}
CAM = {"vid": "0bda", "pid": "5830", "by": "serial", "serial": "200901010001"}


def col(line, text):
    """Return the terminal column where text starts (wide characters take two columns)."""
    head = line[:line.index(text)]
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in head)


def dev(tty, vid="10c4", pid="ea60", serial="0001", port="1-3.2"):
    return m.Device(tty=tty, vid=vid, pid=pid, serial=serial, product="", manufacturer="", port=port)


def test_zero_matches_is_not_plugged_in(env):
    [row] = m.health_check({"usb_esp32": ESP}, [dev("ttyUSB0")], env.dev)
    assert (row.name, row.how, row.ok, row.status) == ("usb_esp32", "10c4:ea60 port 1-3.4", False, "not plugged in")
    assert row.hint == ""


def test_one_match_with_correct_link_is_ok(env):
    os.symlink("ttyUSB0", env.dev / "usb_imu")
    [row] = m.health_check({"usb_imu": IMU}, [dev("ttyUSB0")], env.dev)
    assert (row.how, row.ok, row.status) == ("10c4:ea60 serial 0001", True, "OK  /dev/usb_imu -> ttyUSB0")


def test_one_match_but_link_missing_suggests_reapply(env):
    [row] = m.health_check({"usb_imu": IMU}, [dev("ttyUSB0")], env.dev)
    assert not row.ok
    assert "ttyUSB0" in row.status and "/dev/usb_imu" in row.status
    assert row.hint == "→ choose 4 to re-apply the rules"


def test_one_match_but_link_points_elsewhere(env):
    os.symlink("ttyUSB1", env.dev / "usb_imu")
    [row] = m.health_check({"usb_imu": IMU}, [dev("ttyUSB0"), dev("ttyUSB1", vid="1a86")], env.dev)
    assert not row.ok
    assert "ttyUSB1" in row.status and "ttyUSB0" in row.status


def test_two_matches_warns_and_suggests_rename(env):
    devices = [dev("ttyACM0", "0bda", "5830", "200901010001", "1-1"),
               dev("ttyACM1", "0bda", "5830", "200901010001", "1-2")]
    [row] = m.health_check({"usb_cam": CAM}, devices, env.dev)
    assert not row.ok
    assert row.status == "Warning: matches 2 devices (ttyACM0, ttyACM1); cannot tell which one"
    assert row.hint == ("→ choose 3 to delete it, then name it again with both devices plugged in "
                        "so the physical port is used automatically")


def test_two_matches_on_same_port_explains_multi_interface_chip(env):
    devices = [dev("ttyUSB0", port="1-3.4"), dev("ttyUSB1", port="1-3.4")]
    [row] = m.health_check({"usb_esp32": ESP}, devices, env.dev)
    assert "matches 2 devices" in row.status
    assert "multiple serial ports" in row.hint


def test_late_plugged_twin_is_caught(env):
    # named with only one plugged in (by serial); another with the same serial was plugged in later
    os.symlink("ttyUSB0", env.dev / "usb_imu")
    [row] = m.health_check({"usb_imu": IMU}, [dev("ttyUSB0"), dev("ttyUSB1", port="1-3.4")], env.dev)
    assert not row.ok and "matches 2 devices" in row.status


def test_rows_sorted_by_name(env):
    rows = m.health_check({"b": IMU, "a": ESP}, [], env.dev)
    assert [r.name for r in rows] == ["a", "b"]


def test_format_health_table(env):
    os.symlink("ttyUSB0", env.dev / "usb_imu")
    rows = m.health_check({"usb_imu": IMU, "usb_esp32": ESP}, [dev("ttyUSB0")], env.dev)
    lines = m.format_health(rows).splitlines()
    assert lines[0].split() == ["Name", "Identifier", "Status"]
    assert lines[1].split()[:2] == ["usb_esp32", "10c4:ea60"] and lines[1].endswith("not plugged in")
    assert lines[2].endswith("OK  /dev/usb_imu -> ttyUSB0")
    # the status column starts at the same position on every row
    assert col(lines[1], "not plugged in") == col(lines[2], "OK") == col(lines[0], "Status")


def test_format_health_hint_on_its_own_line_aligned_with_status(env):
    devices = [dev("ttyACM0", "0bda", "5830", "200901010001", "1-1"),
               dev("ttyACM1", "0bda", "5830", "200901010001", "1-2")]
    lines = m.format_health(m.health_check({"usb_cam": CAM}, devices, env.dev)).splitlines()
    assert lines[2].strip().startswith("→ choose 3")
    assert col(lines[2], "→") == col(lines[1], "Warning")


# ---------- WSL ----------
def test_is_wsl(env):
    assert not m.is_wsl(env)
    (env.proc / "version").write_text("Linux version 5.15.153.1-microsoft-standard-WSL2\n")
    assert m.is_wsl(env)


def test_is_wsl_without_proc_version(env):
    (env.proc / "version").unlink()
    assert not m.is_wsl(env)


def add_process(env, pid, comm):
    (env.proc / str(pid)).mkdir()
    (env.proc / str(pid) / "comm").write_text(comm + "\n")


def test_udevd_running(env):
    add_process(env, 1, "systemd")
    assert not m.udevd_running(env)
    add_process(env, 321, "systemd-udevd")
    assert m.udevd_running(env)


def test_wsl_udev_warning_only_on_wsl_without_udevd(env):
    assert m.wsl_udev_warning(env) is None
    (env.proc / "version").write_text("Linux microsoft-standard-WSL2\n")
    warning = m.wsl_udev_warning(env)
    assert "sudo service udev restart" in warning and "/etc/wsl.conf" in warning
    add_process(env, 99, "systemd-udevd")
    assert m.wsl_udev_warning(env) is None
