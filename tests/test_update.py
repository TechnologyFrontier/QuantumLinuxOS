"""Update, trial boot and rollback (QOS-FD 3章, T-OS-06).

The disk is simulated with plain files: two fixed-size "partitions", an ESP
directory and a JSON file standing in for the systemd-boot EFI variables.
A reboot is simulated by consuming the boot loader's one-shot/default entry
and rewriting /proc/cmdline and boot_id.
"""

import hashlib
import json
import os
import tarfile
import uuid

import pytest

from quantumos.admin import bundle as bmod
from quantumos.admin import update as upd
from quantumos.admin.cli import main as admin_main
from quantumos.admin.config import AdminConfig
from quantumos.admin.errors import AdminError
from quantumos.admin.platform import FileBootloader, StateStore

SLOT_SIZE = 1024 * 1024


class Sim:
    def __init__(self, base):
        self.base = base
        for d in ("esp/loader/entries", "state", "proc", "keys", "payload"):
            os.makedirs(os.path.join(base, d), exist_ok=True)
        slots = {}
        for s in ("a", "b"):
            dev = os.path.join(base, f"root-{s}.img")
            with open(dev, "wb") as fh:
                fh.truncate(SLOT_SIZE)
            slots[s] = {"root_device": dev, "root_spec": f"PARTLABEL=qos-root-{s}",
                        "boot_dir": os.path.join(base, "esp", "quantumos", s)}
        priv, pub = bmod.generate_keypair()
        self.priv = priv
        self.pub_path = os.path.join(base, "keys", "update.pub")
        with open(self.pub_path, "wb") as fh:
            fh.write(pub)
        osr = os.path.join(base, "os-release")
        with open(osr, "w") as fh:
            fh.write('ID=quantumos\nQUANTUMOS_VERSION="0.1.0"\n')
        self.health_ok = os.path.join(base, "health-ok")
        open(self.health_ok, "w").close()
        self.cfg = AdminConfig(
            slots=slots,
            state_dir=os.path.join(base, "state"),
            public_key=self.pub_path,
            os_release=osr,
            bootloader="file",
            bootloader_state_file=os.path.join(base, "efivars.json"),
            esp_dir=os.path.join(base, "esp"),
            entries_dir=os.path.join(base, "esp", "loader", "entries"),
            cmdline_path=os.path.join(base, "proc", "cmdline"),
            boot_id_path=os.path.join(base, "proc", "boot_id"),
            health_checks=[{"name": "flag", "argv": ["test", "-e", self.health_ok]}],
            require_root=False,
        )
        self.boot = FileBootloader(self.cfg)
        self.boot.set_default("a")
        self.boot_into("a")

    def boot_into(self, slot):
        with open(self.cfg.cmdline_path, "w") as fh:
            fh.write(f"root=PARTLABEL=qos-root-{slot} ro quantumos.slot={slot} quiet\n")
        with open(self.cfg.boot_id_path, "w") as fh:
            fh.write(str(uuid.uuid4()) + "\n")

    def reboot(self, assess=True):
        nxt = self.boot.next_boot_slot()
        self.boot_into(nxt)
        if assess:
            upd.boot_assess(self.cfg)
        return nxt

    def bundle(self, version="0.2.0", root=b"ROOTFS-v2" * 1000, product="quantumos",
               arch="x86_64", tamper=None, name="update.qosb"):
        paths = {}
        for n, data in (("root.img", root), ("vmlinuz", b"KERNEL" + version.encode()),
                        ("initrd.img", b"INITRD" + version.encode())):
            p = os.path.join(self.base, "payload", n)
            with open(p, "wb") as fh:
                fh.write(data)
            paths[n] = p
        out = os.path.join(self.base, name)
        bmod.create_bundle(out, self.priv, paths, product, arch, version)
        if tamper:
            tamper(out)
        return out

    def state(self):
        return StateStore(self.cfg).load()

    def root_bytes(self, slot, n):
        with open(self.cfg.slots[slot]["root_device"], "rb") as fh:
            return fh.read(n)


@pytest.fixture
def sim(tmp_path):
    return Sim(str(tmp_path))


def _rewrite_member(path, member, transform):
    with tarfile.open(path) as tar:
        items = [(m, tar.extractfile(m).read()) for m in tar.getmembers()]
    import io
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for m, data in items:
            if m.name == member:
                data = transform(data)
                m.size = len(data)
            tar.addfile(m, io.BytesIO(data))


# --- normal update ---------------------------------------------------------
def test_update_trial_and_confirm(sim):
    root = b"ROOTFS-v2" * 1000
    st = upd.update(sim.cfg, sim.bundle(root=root))
    assert (st.state, st.trial_slot) == ("trial_reserved", "b")
    assert sim.root_bytes("b", len(root)) == root
    assert sim.root_bytes("a", 16) == b"\0" * 16  # running slot untouched
    entry = open(os.path.join(sim.cfg.entries_dir, "quantumos-b.conf")).read()
    assert "quantumos.slot=b" in entry and "root=PARTLABEL=qos-root-b ro" in entry

    # assessment in the same boot (no reboot yet) changes nothing
    assert upd.boot_assess(sim.cfg).state == "trial_reserved"

    assert sim.reboot() == "b"
    st = sim.state()
    assert (st.state, st.confirmed_slot, st.confirmed_version) == ("confirmed", "b", "0.2.0")
    assert st.trial_slot is None and st.last_failure is None
    # the confirmed slot is now the default boot entry
    assert sim.reboot() == "b"


def test_second_update_targets_other_slot(sim):
    upd.update(sim.cfg, sim.bundle("0.2.0"))
    sim.reboot()
    st = upd.update(sim.cfg, sim.bundle("0.3.0"))
    assert st.trial_slot == "a"
    assert sim.reboot() == "a"
    assert sim.state().confirmed_version == "0.3.0"


# --- rejected bundles leave everything unchanged -----------------------------
def _assert_untouched(sim):
    assert sim.root_bytes("b", 16) == b"\0" * 16
    assert not os.path.exists(os.path.join(sim.cfg.entries_dir, "quantumos-b.conf"))
    st = sim.state()
    assert (st.state, st.confirmed_slot) == ("confirmed", "a")
    assert sim.reboot(assess=False) == "a"


@pytest.mark.parametrize("kwargs,code", [
    ({"tamper": lambda p: _rewrite_member(p, "root.img", lambda d: d[:-1] + b"X")}, "HASH_MISMATCH"),
    ({"tamper": lambda p: _rewrite_member(p, "manifest.json", lambda d: d.replace(b"0.2.0", b"0.9.0"))},
     "SIGNATURE_INVALID"),
    ({"tamper": lambda p: _rewrite_member(p, "manifest.sig", lambda d: bytes(64))}, "SIGNATURE_INVALID"),
    ({"product": "otheros"}, "PRODUCT_MISMATCH"),
    ({"arch": "arm64"}, "ARCH_MISMATCH"),
    ({"version": "0.1.0"}, "VERSION_REJECTED"),
    ({"version": "0.0.9"}, "VERSION_REJECTED"),
    ({"root": b"x" * (SLOT_SIZE + 1)}, "INSUFFICIENT_SPACE"),
])
def test_rejected_bundles(sim, kwargs, code):
    with pytest.raises(AdminError) as ei:
        upd.update(sim.cfg, sim.bundle(**kwargs))
    assert ei.value.code == code
    _assert_untouched(sim)


def test_bundle_signed_by_other_key(sim):
    sim.priv, _ = bmod.generate_keypair()
    with pytest.raises(AdminError) as ei:
        upd.update(sim.cfg, sim.bundle())
    assert ei.value.code == "SIGNATURE_INVALID"
    _assert_untouched(sim)


def test_bundle_with_extra_member(sim):
    path = sim.bundle()
    with tarfile.open(path, "a") as tar:
        tar.add(sim.pub_path, arcname="../../etc/evil")
    with pytest.raises(AdminError) as ei:
        upd.update(sim.cfg, path)
    assert ei.value.code == "BUNDLE_INVALID"
    _assert_untouched(sim)


def test_concurrent_and_pending_updates_rejected(sim):
    from quantumos.admin.platform import Lock
    with Lock(sim.cfg):
        with pytest.raises(AdminError) as ei:
            upd.update(sim.cfg, sim.bundle())
        assert ei.value.code == "UPDATE_IN_PROGRESS"
    upd.update(sim.cfg, sim.bundle("0.2.0"))
    with pytest.raises(AdminError) as ei:
        upd.update(sim.cfg, sim.bundle("0.3.0", name="u3.qosb"))
    assert ei.value.code == "UPDATE_PENDING"


def test_non_root_rejected(sim):
    sim.cfg.require_root = True
    if os.geteuid() == 0:
        pytest.skip("running as root")
    with pytest.raises(AdminError) as ei:
        upd.update(sim.cfg, sim.bundle())
    assert ei.value.code == "PERMISSION_DENIED"


# --- interrupted write / failed trial → confirmed root and data are kept -------
def test_interrupted_write_boots_confirmed(sim, monkeypatch):
    def boom(*a, **k):
        raise AdminError("WRITE_FAILED", "simulated power loss")
    monkeypatch.setattr(upd, "_install_file", boom)
    with pytest.raises(AdminError):
        upd.update(sim.cfg, sim.bundle())
    st = sim.state()
    assert (st.state, st.trial_slot) == ("confirmed", None)
    assert sim.reboot() == "a"
    assert sim.state().state == "confirmed"


def test_verify_after_write_failure(sim, monkeypatch):
    monkeypatch.setattr(upd, "_hash_prefix", lambda *a: "0" * 64)
    with pytest.raises(AdminError) as ei:
        upd.update(sim.cfg, sim.bundle())
    assert ei.value.code == "VERIFY_FAILED"
    assert sim.reboot() == "a" and sim.state().state == "confirmed"


def test_health_check_failure_rolls_back(sim):
    userdata = os.path.join(sim.cfg.state_dir, "..", "home-alice.txt")
    open(userdata, "w").write("keep me")
    upd.update(sim.cfg, sim.bundle())
    os.unlink(sim.health_ok)
    assert sim.reboot() == "b"
    st = sim.state()
    assert (st.state, st.last_failure, st.confirmed_slot) == ("rolled_back", "HEALTH_CHECK_FAILED", "a")
    assert sim.reboot() == "a"  # next boot goes back to the confirmed root
    assert open(userdata).read() == "keep me"
    # a new update is accepted after the rollback
    open(sim.health_ok, "w").close()
    assert upd.update(sim.cfg, sim.bundle("0.2.1", name="u.qosb")).state == "trial_reserved"


def test_trial_never_comes_up(sim):
    upd.update(sim.cfg, sim.bundle())
    sim.boot.next_boot_slot()  # firmware consumed the one-shot, trial crashed
    sim.boot_into(sim.boot.next_boot_slot())  # watchdog/power cycle → default
    upd.boot_assess(sim.cfg)
    st = sim.state()
    assert (st.state, st.last_failure, st.confirmed_slot) == ("rolled_back", "TRIAL_BOOT_FAILED", "a")


def test_assessment_interrupted(sim):
    upd.update(sim.cfg, sim.bundle())
    sim.reboot(assess=False)
    st = sim.state()
    st.state = "trial_in_progress"  # assessment started, machine hung
    StateStore(sim.cfg).save(st)
    assert sim.reboot() == "a"
    assert sim.state().last_failure == "HEALTH_CHECK_INCOMPLETE"


def test_health_check_timeout(sim):
    sim.cfg.health_checks = [{"name": "hang", "argv": ["sleep", "5"]}]
    sim.cfg.health_check_timeout = 0.3
    upd.update(sim.cfg, sim.bundle())
    sim.reboot()
    assert sim.state().last_failure == "HEALTH_CHECK_TIMEOUT"


# --- update-status ------------------------------------------------------------
def test_update_status_cli(sim, tmp_path, capsys, monkeypatch):
    cfg_path = tmp_path / "admin.json"
    d = {k: getattr(sim.cfg, k) for k in sim.cfg.__dataclass_fields__}
    cfg_path.write_text(json.dumps(d))
    assert admin_main(["--config", str(cfg_path), "--json", "update-status"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["confirmed_slot"] == "a" and info["trial_slot"] is None
    assert admin_main(["--config", str(cfg_path), "update", sim.bundle()]) == 0
    capsys.readouterr()
    assert admin_main(["--config", str(cfg_path), "update-status"]) == 0
    out = capsys.readouterr().out
    assert "trial slot     : b (0.2.0)" in out and "state          : trial_reserved" in out
    assert admin_main(["--config", str(cfg_path), "update", "/nonexistent"]) == 1
    assert "UPDATE_PENDING" in capsys.readouterr().err


def test_bundle_tool_verify(sim, capsys):
    from quantumos.admin.bundletool import main as tool
    path = sim.bundle()
    assert tool(["verify", "--pubkey", sim.pub_path, path]) == 0
    _rewrite_member(path, "vmlinuz", lambda d: d + b"!")
    assert tool(["verify", "--pubkey", sim.pub_path, path]) == 1
