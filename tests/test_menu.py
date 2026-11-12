import json
import os
import subprocess
import sys

import pytest

import usb_udev_namer as m
from conftest import add_usb_tty

APPLY_CMDS = [
    ["sudo", "-n", "udevadm", "control", "--reload-rules"],
    ["sudo", "-n", "udevadm", "trigger", "--subsystem-match=tty", "--action=add"],
]


def answer(env, *answers):
    env.console.answers = list(answers)


def saved_rules(env):
    path = env.config_dir / "rules.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def two_identical_imus(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    add_usb_tty(env.sysfs, "ttyUSB1", "1-3.4")


# ---------- The real external command wrapper ----------
def test_run_cmd_captures_output():
    r = m.run_cmd([sys.executable, "-c", "print('hi')"], timeout=10)
    assert (r.returncode, r.stdout) == (0, "hi\n")


def test_run_cmd_missing_program_is_user_error():
    with pytest.raises(m.UserError, match="Command not found"):
        m.run_cmd(["definitely-not-a-real-program-xyz"], timeout=5)


def test_run_cmd_timeout_is_user_error():
    with pytest.raises(m.UserError, match="timed out"):
        m.run_cmd([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)


def test_run_cmd_never_uses_shell(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    m.run_cmd(["echo", "x"], timeout=3)
    assert isinstance(seen["cmd"], list) and not seen.get("shell") and seen["timeout"] == 3


# ---------- Menu: naming ----------
def test_menu_add_duplicate_devices_uses_port_and_verifies_link(env):
    two_identical_imus(env)
    env.run.links = {"usb_imu": "ttyUSB0"}
    answer(env, "1", "1", "usb_imu", "y", "", "0")
    m.menu(env)
    out = env.console.text
    assert "physical USB port 1-3.2 is used instead" in out and "serial as /dev/ttyUSB1" in out
    assert "Identify by physical USB port instead?" not in out          # already by physical port, no need to ask again
    assert saved_rules(env) == {"usb_imu": {"vid": "10c4", "pid": "ea60", "by": "port", "port": "1-3.2"}}
    assert 'KERNELS=="1-3.2"' in env.udev_rules.read_text()
    assert env.run.calls[0] == ["sudo", "-v"]
    assert env.run.calls[1][:3] == ["sudo", "-n", "cp"] and env.run.calls[1][-1] == str(env.udev_rules)
    assert env.run.calls[2:4] == APPLY_CMDS
    assert env.run.calls[4] == ["ls", "-l", str(env.dev / "usb_imu")]
    assert f"{env.dev / 'usb_imu'} -> ttyUSB0" in out


def test_menu_add_single_device_uses_serial(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    env.run.links = {"usb_imu": "ttyUSB0"}
    answer(env, "1", "1", "usb_imu", "", "y", "", "0")
    m.menu(env)
    assert "Any USB port works" in env.console.text
    assert saved_rules(env)["usb_imu"]["by"] == "serial"


def test_menu_add_can_switch_to_port(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    env.run.links = {"usb_imu": "ttyUSB0"}
    answer(env, "1", "1", "usb_imu", "y", "y", "", "0")
    m.menu(env)
    assert saved_rules(env)["usb_imu"] == {"vid": "10c4", "pid": "ea60", "by": "port", "port": "1-3.2"}


def test_menu_add_declined_confirmation_changes_nothing(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    answer(env, "1", "1", "usb_imu", "", "n", "", "0")
    m.menu(env)
    assert saved_rules(env) is None and env.run.calls == []


def test_menu_add_sudo_failure_keeps_rules_json(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    env.run.fail = {"sudo -v": 1}
    answer(env, "1", "1", "usb_imu", "", "y", "", "0")
    m.menu(env)
    assert "sudo authentication failed" in env.console.text
    assert saved_rules(env) is None
    assert not env.udev_rules.exists()


def test_menu_add_reload_failure_keeps_rules_json(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    env.run.fail = {"--reload-rules": 1}
    answer(env, "1", "1", "usb_imu", "", "y", "", "0")
    m.menu(env)
    assert "--reload-rules failed" in env.console.text
    assert saved_rules(env) is None


def test_menu_add_link_never_appears_is_reported(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    answer(env, "1", "1", "usb_imu", "", "y", "", "0")
    m.menu(env)
    out = env.console.text
    assert "did not appear" in out and "service udev restart" in out
    assert ["ls", "-l", str(env.dev / "usb_imu")] not in env.run.calls
    assert "usb_imu" in saved_rules(env)       # the rule is already in the system, so the record must match


def test_menu_add_bad_name_asks_again(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    env.run.links = {"usb_imu": "ttyUSB0"}
    answer(env, "1", "1", "bad name", "null", "usb_imu", "", "y", "", "0")
    m.menu(env)
    out = env.console.text
    assert "Invalid name 'bad name'" in out and "/dev/null already exists" in out
    assert list(saved_rules(env)) == ["usb_imu"]


def test_menu_add_empty_name_cancels(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    answer(env, "1", "1", "", "", "0")
    m.menu(env)
    assert saved_rules(env) is None and env.run.calls == []


def test_menu_add_bad_device_choice_asks_again(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    answer(env, "1", "9", "x", "0", "", "0")
    m.menu(env)
    assert env.console.text.count("Please enter a number from 0 to 1") == 2


def test_menu_add_without_devices_on_wsl_mentions_wsl_usb_auto(env):
    (env.proc / "version").write_text("microsoft-standard-WSL2")
    answer(env, "1", "", "0")
    m.menu(env)
    out = env.console.text
    assert "No USB serial devices detected" in out and out.count("wsl_usb_auto") == 1


def test_menu_add_with_devices_on_wsl_mentions_wsl_usb_auto_once(env):
    (env.proc / "version").write_text("microsoft-standard-WSL2")
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    answer(env, "1", "0", "", "0")
    m.menu(env)
    assert env.console.text.count("wsl_usb_auto") == 1


def test_menu_add_marks_already_named_device(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    m.save_rules(env, {"usb_imu": {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"}})
    answer(env, "1", "0", "", "0")
    m.menu(env)
    assert "named usb_imu" in env.console.text


# ---------- Menu: other items ----------
def test_menu_header_shows_names_and_alias(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    m.save_rules(env, {"usb_imu": {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"},
                       "usb_gps": {"vid": "1546", "pid": "01a7", "by": "port", "port": "1-1"}})
    os.symlink("ttyUSB0", env.dev / "usb_imu")
    answer(env, "0")
    m.menu(env)
    out = env.console.text
    assert "Names: 2 (usb_gps not plugged in, usb_imu -> ttyUSB0)" in out
    assert "Shortcut: not set" in out
    assert "Choose [0-8]:" in out


def test_menu_unknown_choice_asks_again_and_eof_exits(env):
    answer(env, "abc", "42")
    m.menu(env)        # answers run out → EOF → ends normally, no exception
    assert env.console.text.count("Please enter a number from the menu") == 2


def test_menu_shows_wsl_udev_warning_on_start(env):
    (env.proc / "version").write_text("microsoft-standard-WSL2")
    answer(env, "0")
    m.menu(env)
    assert "sudo service udev restart" in env.console.text


def test_menu_list_shows_health_table(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    m.save_rules(env, {"usb_imu": {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"}})
    answer(env, "2", "", "0")
    m.menu(env)
    assert "/dev/usb_imu did not appear" in env.console.text


def test_menu_remove_name(env):
    m.save_rules(env, {"usb_imu": {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"},
                       "usb_gps": {"vid": "1546", "pid": "01a7", "by": "port", "port": "1-1"}})
    answer(env, "3", "2", "y", "", "0")
    m.menu(env)
    assert list(saved_rules(env)) == ["usb_gps"]
    assert "usb_imu" not in env.udev_rules.read_text() and "usb_gps" in env.udev_rules.read_text()
    assert env.run.calls[2:4] == APPLY_CMDS


def test_menu_remove_without_names(env):
    answer(env, "3", "", "0")
    m.menu(env)
    assert "There are no names yet" in env.console.text and env.run.calls == []


def test_menu_reapply(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    m.save_rules(env, {"usb_imu": {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"}})
    env.run.links = {"usb_imu": "ttyUSB0"}
    answer(env, "4", "", "0")
    m.menu(env)
    assert env.run.calls[2:4] == APPLY_CMDS
    assert "OK  /dev/usb_imu -> ttyUSB0" in env.console.text


def test_menu_scan(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    answer(env, "5", "", "0")
    m.menu(env)
    assert "/dev/ttyUSB0  10c4:ea60" in env.console.text


def test_menu_set_and_remove_alias(env):
    env.bashrc.write_text("export A=1\n")
    answer(env, "6", "", "", "0")
    m.menu(env)
    text = env.bashrc.read_text()
    assert text.startswith("export A=1\n")
    assert "# >>> usb-udev-namer >>>" in text and "alias usbname=" in text
    assert m.get_alias(env) == "usbname"
    answer(env, "7", "y", "", "0")
    m.menu(env)
    assert env.bashrc.read_text().strip() == "export A=1"


def test_set_alias_twice_replaces_block(env):
    m.set_alias(env, "usbname")
    m.set_alias(env, "un")
    assert env.bashrc.read_text().count("# >>> usb-udev-namer >>>") == 1
    assert m.get_alias(env) == "un"


def test_alias_rejects_bad_name(env):
    with pytest.raises(m.UserError):
        m.set_alias(env, "rm -rf")


def test_alias_conflict_needs_confirmation(env):
    env.which = lambda name: "/usr/bin/" + name
    answer(env, "6", "ls", "n", "", "0")
    m.menu(env)
    assert m.get_alias(env) is None


def test_menu_uninstall_needs_two_confirmations(env):
    m.save_rules(env, {"usb_imu": {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"}})
    env.udev_rules.parent.mkdir()
    env.udev_rules.write_text("x")
    answer(env, "8", "y", "n", "", "0")
    m.menu(env)
    assert env.udev_rules.exists() and saved_rules(env) is not None and env.run.calls == []


def test_menu_uninstall_removes_everything(env):
    m.save_rules(env, {"usb_imu": {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"}})
    m.set_alias(env, "usbname")
    env.udev_rules.parent.mkdir()
    env.udev_rules.write_text(m.render_rules({}))
    answer(env, "8", "y", "y", "", "0")
    m.menu(env)
    assert not env.udev_rules.exists()
    assert not env.config_dir.exists()
    assert "usb-udev-namer" not in env.bashrc.read_text()
    assert env.run.calls[1] == ["sudo", "-n", "rm", "-f", str(env.udev_rules)]
    assert env.run.calls[2:4] == APPLY_CMDS


# ---------- Command mode ----------
def test_cli_add(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    env.run.links = {"usb_imu": "ttyUSB0"}
    assert m.main(["add", "/dev/ttyUSB0", "-n", "usb_imu"], env) == 0
    assert saved_rules(env)["usb_imu"]["by"] == "serial"


def test_cli_add_force_port(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    env.run.links = {"usb_imu": "ttyUSB0"}
    assert m.main(["add", "ttyUSB0", "-n", "usb_imu", "--by", "port"], env) == 0
    assert saved_rules(env)["usb_imu"]["by"] == "port"


def test_cli_add_link_missing_exits_nonzero(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    assert m.main(["add", "/dev/ttyUSB0", "-n", "usb_imu"], env) == 1


def test_cli_add_unknown_device(env):
    assert m.main(["add", "/dev/ttyUSB7", "-n", "usb_imu"], env) == 1
    assert "Cannot find /dev/ttyUSB7" in env.console.text


def test_cli_remove_unknown_name(env):
    assert m.main(["remove", "nope"], env) == 1
    assert "No name called 'nope'" in env.console.text


def test_cli_list_and_scan(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    assert m.main(["scan"], env) == 0
    assert m.main(["list"], env) == 0
    assert "There are no names yet" in env.console.text


def test_cli_apply(env):
    m.save_rules(env, {"usb_imu": {"vid": "10c4", "pid": "ea60", "by": "serial", "serial": "0001"}})
    assert m.main(["apply"], env) == 0
    assert env.run.calls[2:4] == APPLY_CMDS


def test_cli_uninstall_asks_twice(env):
    m.save_rules(env, {})
    answer(env, "y", "y")
    assert m.main(["uninstall"], env) == 0
    assert not env.config_dir.exists()


def test_cli_broken_rules_json_is_reported(env):
    env.config_dir.mkdir()
    (env.config_dir / "rules.json").write_text("{")
    assert m.main(["list"], env) == 1
    assert "rules.json" in env.console.text


def test_main_without_args_opens_menu_and_ctrl_c_exits_cleanly(env):
    def interrupt(prompt):
        raise KeyboardInterrupt
    env.ask = interrupt
    assert m.main([], env) == 0


def test_cli_ctrl_c_exits_130(env):
    def interrupt(prompt):
        raise KeyboardInterrupt
    env.ask = interrupt
    assert m.main(["uninstall"], env) == 130


def test_root_runs_without_sudo(env):
    env.is_root = True
    m.save_rules(env, {})
    assert m.main(["apply"], env) == 0
    assert env.run.calls[0][0] == "cp"
    assert ["udevadm", "control", "--reload-rules"] in env.run.calls


# ---------- Error paths added after review ----------
def test_link_pointing_to_wrong_tty_is_reported_as_failure(env):
    two_identical_imus(env)
    env.run.links = {"usb_imu": "ttyUSB1"}       # udev actually gave the name to another device
    assert m.main(["add", "/dev/ttyUSB0", "-n", "usb_imu"], env) == 1
    assert "points to ttyUSB1" in env.console.text


def test_cp_failure_keeps_rules_json(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    env.run.fail = {" cp ": 1}
    assert m.main(["add", "/dev/ttyUSB0", "-n", "usb_imu"], env) == 1
    assert saved_rules(env) is None


def test_trigger_failure_keeps_rules_json(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    env.run.fail = {"trigger": 1}
    assert m.main(["add", "/dev/ttyUSB0", "-n", "usb_imu"], env) == 1
    assert saved_rules(env) is None


def test_menu_non_ascii_digit_does_not_crash(env):
    answer(env, "²", "0")
    m.menu(env)
    assert "Please enter a number from the menu" in env.console.text


def test_device_pick_non_ascii_digit_does_not_crash(env):
    add_usb_tty(env.sysfs, "ttyUSB0", "1-3.2")
    answer(env, "1", "²", "0", "", "0")
    m.menu(env)
    assert "Please enter a number from 0 to 1" in env.console.text


def test_bashrc_with_non_utf8_bytes_is_preserved(env):
    env.bashrc.write_bytes(b"export X='\xff\xfe'\n")
    m.set_alias(env, "usbname")
    assert m.get_alias(env) == "usbname"
    m.remove_alias(env)
    assert env.bashrc.read_bytes() == b"export X='\xff\xfe'\n"


def test_foreign_file_with_same_name_is_not_overwritten(env):
    env.udev_rules.parent.mkdir()
    env.udev_rules.write_text('SUBSYSTEM=="tty", SYMLINK+="mine"\n')
    m.save_rules(env, {})
    assert m.main(["apply"], env) == 1
    assert "was not generated by this tool" in env.console.text
    assert env.udev_rules.read_text() == 'SUBSYSTEM=="tty", SYMLINK+="mine"\n'


def test_foreign_file_with_same_name_is_not_deleted(env):
    env.udev_rules.parent.mkdir()
    env.udev_rules.write_text("# someone else's\n")
    answer(env, "y", "y")
    assert m.main(["uninstall"], env) == 1
    assert env.udev_rules.exists() and env.run.calls == []


def test_rules_file_written_by_earlier_version_is_still_ours(env):
    env.udev_rules.parent.mkdir()
    env.udev_rules.write_text("# \u7531 usb_udev_namer.py \u81ea\u52d5\u7522\u751f\n", encoding="utf-8")
    m.save_rules(env, {})
    assert m.main(["apply"], env) == 0
    assert "was not generated by this tool" not in env.console.text
