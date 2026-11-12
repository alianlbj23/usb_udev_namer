import pytest

import usb_udev_namer as m


def dev(tty="ttyUSB0", vid="10c4", pid="ea60", serial="0001", port="1-3.2"):
    return m.Device(tty=tty, vid=vid, pid=pid, serial=serial, product="", manufacturer="", port=port)


def test_unique_serial_uses_serial():
    d = dev()
    choice = m.choose_match(d, [d, dev("ttyUSB1", vid="1a86", pid="7523", port="1-4")])
    assert choice.rule == {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"}
    assert choice.twin is None
    assert choice.explain() == "Identified by: VID:PID 10c4:ea60 + serial 0001. Any USB port works."


def test_same_vid_pid_different_serial_still_uses_serial():
    a, b = dev(), dev("ttyUSB1", serial="0002", port="1-3.4")
    assert m.choose_match(a, [a, b]).rule["by"] == "serial"


def test_duplicate_serial_falls_back_to_port():
    a, b = dev(), dev("ttyUSB1", port="1-3.4")
    choice = m.choose_match(a, [a, b])
    assert choice.rule == {"vid": "10c4", "pid": "ea60", "by": "port", "port": "1-3.2"}
    assert choice.twin == b
    assert choice.explain() == (
        "Identified by: same VID:PID and serial as /dev/ttyUSB1, so physical USB port 1-3.2 is used instead.\n"
        "               Always plug it into the same port; rename it after moving to another port.")


def test_missing_serial_falls_back_to_port():
    d = dev(serial="")
    choice = m.choose_match(d, [d])
    assert choice.rule == {"vid": "10c4", "pid": "ea60", "by": "port", "port": "1-3.2"}
    assert "has no serial number" in choice.explain()
    assert "plug it into the same port" in choice.explain()


def test_force_port_even_when_serial_unique():
    d = dev()
    choice = m.choose_match(d, [d], by="port")
    assert choice.rule["by"] == "port"
    assert "plug it into the same port" in choice.explain()


def test_force_serial_without_serial_is_an_error():
    d = dev(serial="")
    with pytest.raises(m.UserError, match="has no serial number"):
        m.choose_match(d, [d], by="serial")


def test_force_serial_with_duplicate_warns_loudly():
    a, b = dev(), dev("ttyUSB1", port="1-3.4")
    choice = m.choose_match(a, [a, b], by="serial")
    assert choice.rule["by"] == "serial"
    assert "Warning" in choice.explain() and "ttyUSB1" in choice.explain()


def test_unknown_by_is_an_error():
    d = dev()
    with pytest.raises(m.UserError):
        m.choose_match(d, [d], by="vidpid")


def test_matches_serial_rule():
    rule = {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"}
    assert m.matches(rule, dev())
    assert m.matches(rule, dev(port="2-1"))
    assert not m.matches(rule, dev(serial="0002"))
    assert not m.matches(rule, dev(pid="ea61"))


def test_matches_port_rule():
    rule = {"vid": "10c4", "pid": "ea60", "by": "port", "port": "1-3.2"}
    assert m.matches(rule, dev(serial="whatever"))
    assert not m.matches(rule, dev(port="1-3.4"))
    assert not m.matches(rule, dev(vid="1a86"))


def test_multi_tty_on_same_usb_device_is_refused():
    # dual-port chip: one USB device (same port, same serial) exposes two ttys, so any rule matches both
    a, b = dev("ttyUSB0", port="1-3.4"), dev("ttyUSB1", port="1-3.4")
    for by in ("auto", "serial", "port"):
        with pytest.raises(m.UserError, match="multiple serial ports"):
            m.choose_match(a, [a, b], by=by)
