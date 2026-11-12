import os

import usb_udev_namer as m
from conftest import add_usb_tty


def test_empty_sysfs_returns_no_devices(env):
    assert m.scan_devices(env.sysfs) == []


def test_missing_tty_class_dir_returns_no_devices(tmp_path):
    assert m.scan_devices(tmp_path / "nothing") == []


def test_ttyusb_reads_usb_attributes_and_port(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    [d] = m.scan_devices(env.sysfs)
    assert d == m.Device(tty="ttyUSB0", vid="10c4", pid="ea60", serial="0001",
                         product="CP2102 USB to UART Bridge Controller",
                         manufacturer="Silicon Labs", port="1-3.2")


def test_ttyacm_device_link_points_at_interface(env):
    add_usb_tty(env.sysfs, "ttyACM0", "3-1", vid="2341", pid="0043", serial="ABC123",
                product="Arduino Uno", manufacturer="Arduino")
    [d] = m.scan_devices(env.sysfs)
    assert (d.tty, d.vid, d.pid, d.serial, d.port) == ("ttyACM0", "2341", "0043", "ABC123", "3-1")


def test_missing_serial_becomes_empty_string(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-2", serial=None, product=None, manufacturer=None)
    [d] = m.scan_devices(env.sysfs)
    assert (d.serial, d.product, d.manufacturer) == ("", "", "")


def test_unsafe_serial_is_treated_as_missing(env):
    # udev's == is a glob match, so a serial containing * or quotes cannot be written into a rule safely
    add_usb_tty(env.sysfs, "ttyUSB0", "1-2", serial='ab"c*')
    [d] = m.scan_devices(env.sysfs)
    assert d.serial == ""


def test_non_usb_ttys_are_ignored(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-2")
    for name in ("ttyS0", "tty1", "console"):
        os.makedirs(env.sysfs / "class" / "tty" / name)
    assert [d.tty for d in m.scan_devices(env.sysfs)] == ["ttyUSB0"]


def test_tty_without_usb_parent_is_skipped(env):
    # e.g. the device link is broken or it is not a USB device
    (env.sysfs / "class" / "tty" / "ttyUSB9").mkdir()
    assert m.scan_devices(env.sysfs) == []


def test_devices_sorted_naturally(env):
    add_usb_tty(env.sysfs, "ttyUSB10", "1-4")
    add_usb_tty(env.sysfs, "ttyUSB2", "1-3")
    add_usb_tty(env.sysfs, "ttyACM0", "1-5")
    assert [d.tty for d in m.scan_devices(env.sysfs)] == ["ttyACM0", "ttyUSB2", "ttyUSB10"]


def test_device_label_shows_everything_students_need(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    [d] = m.scan_devices(env.sysfs)
    assert m.device_label(d) == ("/dev/ttyUSB0  10c4:ea60  Silicon Labs CP2102 USB to UART Bridge Controller"
                                 "  serial 0001  port 1-3.2")


def test_device_label_without_serial(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-2", serial=None, manufacturer=None, product="USB Serial")
    [d] = m.scan_devices(env.sysfs)
    assert m.device_label(d) == "/dev/ttyUSB0  10c4:ea60  USB Serial  no serial  port 1-2"


def test_device_with_malformed_ids_is_skipped(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-2", vid="zz")
    add_usb_tty(env.sysfs, "ttyUSB1", "1-3")
    assert [d.tty for d in m.scan_devices(env.sysfs)] == ["ttyUSB1"]


def test_tty_name_with_trailing_newline_is_ignored(env):
    assert m.USB_TTY_RE.match("ttyUSB0\n") is None
