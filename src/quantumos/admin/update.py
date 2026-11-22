"""Update, boot assessment and rollback (QOS-FD OS-06/07, QOS-IF 5章)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time

from .. import protocol
from . import bundle as bundlemod
from .config import AdminConfig
from .errors import AdminError
from .platform import (
    CONFIRMED,
    ROLLED_BACK,
    TRIAL_IN_PROGRESS,
    TRIAL_RESERVED,
    Lock,
    StateStore,
    UpdateState,
    atomic_write,
    boot_id,
    booted_slot,
    make_bootloader,
    read_os_version,
)

_CHUNK = 4 * 1024 * 1024


def _require_root(cfg: AdminConfig) -> None:
    if cfg.require_root and os.geteuid() != 0:
        raise AdminError("PERMISSION_DENIED", "administrator privileges are required")


def load_or_init_state(cfg: AdminConfig, store: StateStore) -> UpdateState:
    """Initialise the state on first use from the slot that is running."""
    if store.exists():
        return store.load()
    st = UpdateState(confirmed_slot=booted_slot(cfg), confirmed_version=read_os_version(cfg))
    store.save(st)
    return st


# --------------------------------------------------------------------------
# qos-admin update BUNDLE
# --------------------------------------------------------------------------
def _device_capacity(path: str) -> int:
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def _write_device(src, size: int, dev: str) -> None:
    """Write ``size`` bytes into a block device (or pre-allocated file)."""
    try:
        fd = os.open(dev, os.O_WRONLY)
    except OSError as exc:
        raise AdminError("WRITE_FAILED", f"cannot open {dev}: {exc.strerror}") from exc
    try:
        remaining = size
        while remaining:
            chunk = src.read(min(_CHUNK, remaining))
            if not chunk:
                raise AdminError("WRITE_FAILED", "bundle ended early")
            view = memoryview(chunk)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            remaining -= len(chunk)
        os.fsync(fd)
    except OSError as exc:
        raise AdminError("WRITE_FAILED", f"write to {dev} failed: {exc.strerror}") from exc
    finally:
        os.close(fd)


def _hash_prefix(path: str, size: int) -> str:
    # O_DIRECT would bypass the page cache; dropping caches is left to the
    # platform integration.  posix_fadvise reduces stale-cache reads.
    with open(path, "rb") as fh:
        try:
            os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        return bundlemod.sha256_stream(fh, size)


def _install_file(src, size: int, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    with open(tmp, "wb") as out:
        remaining = size
        while remaining:
            chunk = src.read(min(_CHUNK, remaining))
            if not chunk:
                raise AdminError("WRITE_FAILED", "bundle ended early")
            out.write(chunk)
            remaining -= len(chunk)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, dest)


def update(cfg: AdminConfig, bundle_path: str, log=lambda msg: None) -> UpdateState:
    _require_root(cfg)
    store = StateStore(cfg)
    boot = make_bootloader(cfg)
    with Lock(cfg):
        st = load_or_init_state(cfg, store)
        if st.state in (TRIAL_RESERVED, TRIAL_IN_PROGRESS):
            raise AdminError(
                "UPDATE_PENDING", f"a trial of slot {st.trial_slot} is already {st.state}"
            )
        current = booted_slot(cfg)
        if current != st.confirmed_slot:
            raise AdminError(
                "SLOT_STATE_INVALID",
                f"running slot {current} is not the confirmed slot {st.confirmed_slot}",
            )
        target = cfg.other_slot(current)
        slot = cfg.slots[target]

        # 1. verification — nothing is changed unless all checks pass
        key = bundlemod.load_public_key(cfg.public_key)
        with bundlemod.Bundle(bundle_path) as b:
            man = b.verified_manifest(key)
            log(f"signature OK: {man.product_id} {man.architecture} {man.version}")
            if man.product_id != cfg.product_id:
                raise AdminError("PRODUCT_MISMATCH", f"bundle is for {man.product_id}")
            if man.architecture != cfg.architecture:
                raise AdminError("ARCH_MISMATCH", f"bundle is for {man.architecture}")
            if bundlemod.parse_version(man.version) <= bundlemod.parse_version(
                st.confirmed_version
            ):
                raise AdminError(
                    "VERSION_REJECTED",
                    f"bundle version {man.version} is not newer than {st.confirmed_version}",
                )
            b.check_payload_hashes(man)
            log("payload hashes OK")
            root_size = man.files["root.img"][1]
            try:
                capacity = _device_capacity(slot["root_device"])
            except OSError as exc:
                raise AdminError("WRITE_FAILED", f"cannot open {slot['root_device']}") from exc
            if capacity < root_size:
                raise AdminError("INSUFFICIENT_SPACE", f"slot {target} is smaller than root.img")
            boot_bytes = man.files["vmlinuz"][1] + man.files["initrd.img"][1]
            os.makedirs(slot["boot_dir"], exist_ok=True)
            # files are staged next to the old ones before being renamed
            if shutil.disk_usage(slot["boot_dir"]).free < boot_bytes:
                raise AdminError("INSUFFICIENT_SPACE", "not enough space on the ESP")

            # 2. write the inactive slot.  The running root is never touched;
            # the target entry is removed first so an interrupted write can
            # never be selected.
            boot.remove_entry(target)
            log(f"writing slot {target}")
            _write_device(b.open("root.img"), root_size, slot["root_device"])
            for name in ("vmlinuz", "initrd.img"):
                _install_file(
                    b.open(name), man.files[name][1], os.path.join(slot["boot_dir"], name)
                )

        # 3. re-verify what was written
        if _hash_prefix(slot["root_device"], root_size) != man.files["root.img"][0]:
            raise AdminError("VERIFY_FAILED", f"slot {target} root does not match after write")
        for name in ("vmlinuz", "initrd.img"):
            path = os.path.join(slot["boot_dir"], name)
            with open(path, "rb") as fh:
                if bundlemod.sha256_stream(fh) != man.files[name][0]:
                    raise AdminError("VERIFY_FAILED", f"{name} does not match after write")
        log("written data verified")

        # 4. reserve the trial (one-shot boot); no automatic reboot
        boot.write_entry(target, man.version)
        boot.set_oneshot(target)
        st.state = TRIAL_RESERVED
        st.trial_slot = target
        st.trial_version = man.version
        st.reserved_boot_id = boot_id(cfg)
        store.save(st)
        log(f"trial boot of slot {target} reserved; reboot to try it")
        return st


# --------------------------------------------------------------------------
# qos-admin update-status
# --------------------------------------------------------------------------
def status(cfg: AdminConfig) -> dict:
    store = StateStore(cfg)
    st = store.load() if store.exists() else None
    try:
        running = booted_slot(cfg)
    except AdminError:
        running = None
    if st is None:
        return {
            "confirmed_slot": running,
            "trial_slot": None,
            "state": CONFIRMED,
            "last_failure": None,
            "booted_slot": running,
            "confirmed_version": read_os_version(cfg),
            "trial_version": None,
        }
    return {
        "confirmed_slot": st.confirmed_slot,
        "trial_slot": st.trial_slot,
        "state": st.state,
        "last_failure": st.last_failure,
        "booted_slot": running,
        "confirmed_version": st.confirmed_version,
        "trial_version": st.trial_version,
    }


# --------------------------------------------------------------------------
# Health checks used for the trial boot
# --------------------------------------------------------------------------
def _mount_options(cfg: AdminConfig, mountpoint: str) -> list[str] | None:
    found = None
    try:
        with open(cfg.mounts_path) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == mountpoint:
                    found = parts[3].split(",")  # last one wins (overmounts)
    except OSError:
        return None
    return found


def _check_root_readonly(cfg: AdminConfig) -> bool:
    opts = _mount_options(cfg, "/")
    return opts is not None and "ro" in opts


def _check_state_writable(cfg: AdminConfig) -> bool:
    probe = os.path.join(cfg.state_dir, ".health-probe")
    try:
        atomic_write(probe, b"ok\n", 0o600)
        os.unlink(probe)
        return True
    except OSError:
        return False


def probe_socket(path: str, timeout: float = 10.0) -> bool:
    """True if the service answers a status request with any valid protocol
    response (``ACCESS_DENIED`` for a non-member caller still proves it is
    serving)."""
    req = b'{"protocol_version":1,"op":"status"}'
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            protocol.write_frame(s, req)
            resp = protocol.parse_response(protocol.read_frame(s, timeout))
    except Exception:  # noqa: BLE001
        return False
    return resp["ok"] or resp["error"]["code"] == "ACCESS_DENIED"


_BUILTINS = {
    "root_readonly": _check_root_readonly,
    "state_writable": _check_state_writable,
    "socket_probe": lambda cfg: probe_socket(cfg.socket_path),
}


def run_health_checks(cfg: AdminConfig, log=lambda msg: None) -> str | None:
    """Return ``None`` when healthy or a failure code."""
    deadline = time.monotonic() + cfg.health_deadline
    for check in cfg.health_checks:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "HEALTH_CHECK_TIMEOUT"
        name = check.get("name", "?")
        if "builtin" in check:
            ok = _BUILTINS[check["builtin"]](cfg)
        else:
            try:
                r = subprocess.run(
                    check["argv"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=min(cfg.health_check_timeout, remaining),
                )
                ok = r.returncode == 0
            except subprocess.TimeoutExpired:
                log(f"health check {name}: timeout")
                return "HEALTH_CHECK_TIMEOUT"
            except OSError:
                ok = False
        log(f"health check {name}: {'ok' if ok else 'FAILED'}")
        if not ok:
            return "HEALTH_CHECK_FAILED"
    return None


# --------------------------------------------------------------------------
# Boot assessment (run once per boot by qos-boot-assess.service)
# --------------------------------------------------------------------------
def boot_assess(cfg: AdminConfig, log=lambda msg: None) -> UpdateState:
    _require_root(cfg)
    store = StateStore(cfg)
    boot = make_bootloader(cfg)
    with Lock(cfg):
        st = load_or_init_state(cfg, store)
        current = booted_slot(cfg)
        this_boot = boot_id(cfg)

        def rollback(code: str) -> UpdateState:
            log(f"trial of slot {st.trial_slot} failed ({code}); "
                f"next boot uses confirmed slot {st.confirmed_slot}")
            boot.set_oneshot(None)
            boot.set_default(st.confirmed_slot)
            st.state = ROLLED_BACK
            st.last_failure = code
            st.trial_slot = None
            st.trial_version = None
            st.reserved_boot_id = None
            store.save(st)
            return st

        if st.state == TRIAL_RESERVED:
            if this_boot and this_boot == st.reserved_boot_id:
                log("trial reserved; waiting for reboot")
                return st
            if current == st.confirmed_slot:
                # the one-shot was consumed but the trial never reached
                # assessment: firmware/boot loader fell back
                return rollback("TRIAL_BOOT_FAILED")
            if current != st.trial_slot:
                return rollback("TRIAL_BOOT_FAILED")
            st.state = TRIAL_IN_PROGRESS
            store.save(st)
            code = run_health_checks(cfg, log)
            if code:
                return rollback(code)
            boot.set_default(current)
            log(f"slot {current} ({st.trial_version}) confirmed")
            st.state = CONFIRMED
            st.confirmed_slot = current
            st.confirmed_version = st.trial_version or st.confirmed_version
            st.trial_slot = None
            st.trial_version = None
            st.reserved_boot_id = None
            st.last_failure = None
            store.save(st)
            return st

        if st.state == TRIAL_IN_PROGRESS:
            # a previous boot started assessing but never finished
            return rollback("HEALTH_CHECK_INCOMPLETE")

        if current != st.confirmed_slot:
            # booted a non-confirmed slot outside of a trial (manual choice)
            log(f"warning: running slot {current} is not the confirmed slot")
        return st
