import json
import os
import shutil
import subprocess

import pytest

import usb_udev_namer as m

IMU = {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"}
ESP = {"vid": "10c4", "pid": "ea60", "by": "port", "port": "1-3.4"}


# ---------- Rule text ----------
def test_rule_line_by_serial():
    assert m.rule_line("usb_imu", IMU) == (
        'SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", '
        'ATTRS{serial}=="0001", SYMLINK+="usb_imu", MODE="0666"')


def test_rule_line_by_port():
    assert m.rule_line("usb_esp32", ESP) == (
        'SUBSYSTEM=="tty", KERNELS=="1-3.4", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", '
        'SYMLINK+="usb_esp32", MODE="0666"')


def test_render_rules_has_do_not_edit_header_and_sorted_lines():
    text = m.render_rules({"usb_imu": IMU, "usb_esp32": ESP})
    lines = text.splitlines()
    assert lines[0].startswith("#") and "Do not edit by hand" in text
    rule_lines = [s for s in lines if s and not s.startswith("#")]
    assert rule_lines == [m.rule_line("usb_esp32", ESP), m.rule_line("usb_imu", IMU)]
    assert text.endswith("\n")


def test_render_empty_rules_is_only_header():
    assert all(s.startswith("#") or not s for s in m.render_rules({}).splitlines())


# ---------- rules.json ----------
def test_load_rules_when_file_missing_is_empty(env):
    assert m.load_rules(env) == {}


def test_save_then_load_round_trip(env):
    m.save_rules(env, {"usb_imu": IMU})
    assert m.load_rules(env) == {"usb_imu": IMU}
    assert json.loads(m.rules_file(env).read_text(encoding="utf-8")) == {"usb_imu": IMU}


def test_save_leaves_no_temp_files(env):
    m.save_rules(env, {"usb_imu": IMU})
    assert os.listdir(env.config_dir) == ["rules.json"]


def test_load_rules_rejects_broken_json(env):
    env.config_dir.mkdir()
    m.rules_file(env).write_text("{not json", encoding="utf-8")
    with pytest.raises(m.UserError, match="rules.json"):
        m.load_rules(env)


@pytest.mark.parametrize("bad", [
    [],
    {"usb_imu": "x"},
    {"bad name!": IMU},
    {"usb_imu": dict(IMU, vid="10C4X")},
    {"usb_imu": dict(IMU, by="vidpid")},
    {"usb_imu": dict(IMU, serial='a"b')},
    {"usb_imu": {"vid": "10c4", "pid": "ea60", "by": "port", "port": "1-3; rm"}},
    {"usb_imu": {"vid": "10c4", "pid": "ea60", "by": "serial"}},
])
def test_load_rules_rejects_hand_edited_garbage(env, bad):
    env.config_dir.mkdir()
    m.rules_file(env).write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(m.UserError, match="rules.json"):
        m.load_rules(env)


# ---------- Name validation ----------
@pytest.mark.parametrize("name", ["usb_imu", "IMU", "a", "x.y-z_1", "0imu", "a" * 32])
def test_valid_names(env, name):
    assert m.validate_name(name, {}, env.dev) == name


@pytest.mark.parametrize("name", ["", "-imu", ".imu", "a b", "a/b", "\u4e2d\u6587", "a" * 33, "imu!"])
def test_invalid_names(env, name):
    with pytest.raises(m.UserError, match="Invalid name"):
        m.validate_name(name, {}, env.dev)


def test_name_already_in_rules(env):
    with pytest.raises(m.UserError, match="already taken"):
        m.validate_name("usb_imu", {"usb_imu": IMU}, env.dev)


def test_name_clashes_with_existing_dev_entry(env):
    with pytest.raises(m.UserError, match="/dev/null"):
        m.validate_name("null", {}, env.dev)


def test_name_clashes_with_dangling_dev_symlink(env):
    os.symlink("ttyUSB9", env.dev / "old_link")
    with pytest.raises(m.UserError, match="already exists"):
        m.validate_name("old_link", {}, env.dev)


@pytest.mark.parametrize("name", ["ttyUSB0", "ttyACM3", "ttyS0", "tty1", "sda", "sdb1", "video0", "hidraw2"])
def test_kernel_device_names_are_reserved_even_when_unplugged(env, name):
    with pytest.raises(m.UserError, match="system devices"):
        m.validate_name(name, {}, env.dev)


def test_name_with_trailing_newline_is_invalid(env):
    with pytest.raises(m.UserError, match="Invalid name"):
        m.validate_name("usb_imu\n", {}, env.dev)


def test_rules_json_key_with_trailing_newline_is_rejected(env):
    env.config_dir.mkdir()
    m.rules_file(env).write_text(json.dumps({"usb_imu\n": IMU}), encoding="utf-8")
    with pytest.raises(m.UserError, match="rules.json"):
        m.load_rules(env)


@pytest.mark.skipif(not shutil.which("udevadm"), reason="udevadm is not available")
def test_generated_rules_pass_real_udevadm_verify(tmp_path):
    r = subprocess.run(["udevadm", "verify", "--help"], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        pytest.skip("udevadm is too old to have verify")
    path = tmp_path / "99-usb-udev-namer.rules"
    path.write_text(m.render_rules({"usb_imu": IMU, "usb_esp32": ESP}), encoding="utf-8")
    r = subprocess.run(["udevadm", "verify", "--no-style", str(path)],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stdout + r.stderr
