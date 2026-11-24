"""Static checks of the desktop edition's image files (QOS-DT 2章, T-DT-04/06)."""

import configparser
import os
import re
import subprocess
import xml.etree.ElementTree as ET

from quantumos.admin.config import AdminConfig

REPO = os.path.join(os.path.dirname(__file__), "..")
ED = os.path.join(REPO, "image", "editions", "desktop")
SYS = os.path.join(REPO, "system", "desktop")


def _env(edition, **extra):
    """Values of config.env as the build scripts see them."""
    script = f'. "{REPO}/image/config.env"; echo "$QOS_PRODUCT_ID $ROOT_SLOT_SIZE_MIB $ROOT_IMAGE_SIZE_MIB $DISK_SIZE_MIB"'
    env = {"PATH": os.environ["PATH"], **extra}
    if edition:
        env["QOS_EDITION"] = edition
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)


def test_editions_in_config_env():
    assert _env(None).stdout.split() == ["quantumos", "6144", "3072", "32768"]
    assert _env("desktop").stdout.split() == ["quantumos-desktop", "12288", "6144", "65536"]
    # explicit environment values still win
    assert _env("desktop", ROOT_IMAGE_SIZE_MIB="10000").stdout.split()[2] == "10000"
    bad = _env("nosuch")
    assert bad.returncode != 0 and "unknown QOS_EDITION" in bad.stderr


def test_desktop_update_conf():
    cfg = AdminConfig.load(os.path.join(ED, "update.conf"))
    assert cfg.product_id == "quantumos-desktop"
    names = [c["name"] for c in cfg.health_checks]
    assert "display-manager" in names and "console" not in names
    std = AdminConfig.load(os.path.join(REPO, "system", "config", "update.conf"))
    # the desktop list is the standard list with the console replaced by GDM
    assert [n for n in names if n != "display-manager"] == \
        [c["name"] for c in std.health_checks if c["name"] != "console"]


def test_polkit_actions_select_the_helper_by_first_argument():
    root = ET.parse(os.path.join(SYS, "polkit", "org.quantumos.update.policy")).getroot()
    actions = {a.get("id"): a for a in root.findall("action")}
    assert set(actions) == {"org.quantumos.update.status", "org.quantumos.update.apply"}
    for aid, argv1 in (("org.quantumos.update.status", "status"), ("org.quantumos.update.apply", "apply")):
        ann = {a.get("key"): a.text for a in actions[aid].findall("annotate")}
        assert ann["org.freedesktop.policykit.exec.path"] == "/usr/libexec/quantumos/qos-update-helper"
        assert ann["org.freedesktop.policykit.exec.argv1"] == argv1
    apply = actions["org.quantumos.update.apply"].find("defaults")
    assert apply.findtext("allow_active") == "auth_admin"  # never cached, never anonymous
    assert apply.findtext("allow_any") == "no"
    with open(os.path.join(SYS, "polkit", "50-quantumos-update.rules")) as fh:
        code = "".join(ln for ln in fh if not ln.lstrip().startswith("//"))
    # only reading the state is granted without a password
    assert "org.quantumos.update.status" in code and "update.apply" not in code


def test_desktop_entries_point_at_installed_commands():
    configure = open(os.path.join(ED, "configure.sh")).read()
    commands = set(re.findall(r"(qos-[a-z-]+):quantumos\.desktop\.(\w+)", configure))
    assert {c for c, _ in commands} == {"qos-studio", "qos-update-manager", "qos-devenv-setup"}
    for _, module in commands:
        assert os.path.exists(os.path.join(REPO, "src", "quantumos", "desktop", module + ".py"))
    icons = set(os.listdir(os.path.join(SYS, "icons")))
    for name in os.listdir(os.path.join(SYS, "applications")):
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        cp.read(os.path.join(SYS, "applications", name), encoding="utf-8")
        e = cp["Desktop Entry"]
        assert e["Exec"].split()[0].removeprefix("/usr/bin/") in {c for c, _ in commands}
        assert e["Icon"] + ".svg" in icons
        assert name == e["Icon"] + ".desktop"


def test_gnome_favorites_include_the_quantum_apps():
    text = open(os.path.join(ED, "90_quantumos.gschema.override")).read()
    for app in os.listdir(os.path.join(SYS, "applications")):
        if "DevSetup" not in app:
            assert app in text


def test_desktop_packages_include_runtime_of_the_apps():
    pkgs = {ln.strip() for ln in open(os.path.join(ED, "packages.txt")) if ln.strip() and not ln.startswith("#")}
    assert {"gnome-core", "python3-gi", "python3-gi-cairo", "gir1.2-gtk-4.0", "gir1.2-adw-1",
            "pkexec", "polkitd", "flatpak"} <= pkgs


def test_scripts_parse():
    for path in ("image/build-rootfs.sh", "image/editions/desktop/configure.sh"):
        subprocess.run(["bash", "-n", os.path.join(REPO, path)], check=True)


def test_trial_assessment_waits_for_the_display_manager():
    # found in VM evaluation: without this the check raced GDM and rolled back
    conf = configparser.ConfigParser()
    conf.read(os.path.join(ED, "qos-boot-assess-desktop.conf"))
    assert "display-manager.service" in conf["Unit"]["After"].split()
    configure = open(os.path.join(ED, "configure.sh")).read()
    assert "qos-boot-assess.service.d/50-desktop.conf" in configure


def test_system_partitions_hidden_from_udisks():
    rule = open(os.path.join(ED, "80-quantumos-udisks.rules")).read()
    for label in ("ESP", "qos-root-a", "qos-root-b", "qos-state"):
        assert label in rule
    assert 'ENV{UDISKS_IGNORE}="1"' in rule
    assert "80-quantumos-udisks.rules" in open(os.path.join(ED, "configure.sh")).read()


def test_updated_slots_keep_the_install_kernel_options():
    # found in VM evaluation: entries written by qos-admin lacked the consoles
    out = subprocess.run(["bash", "-c", f'. "{REPO}/image/config.env"; echo "$KERNEL_CMDLINE_EXTRA"'],
                         capture_output=True, text=True, env={"PATH": os.environ["PATH"]}).stdout.strip()
    for conf in (os.path.join(ED, "update.conf"), os.path.join(REPO, "system", "config", "update.conf")):
        assert AdminConfig.load(conf).kernel_cmdline_extra == out
