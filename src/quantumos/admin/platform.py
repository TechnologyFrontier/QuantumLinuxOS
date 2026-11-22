"""Platform access: update state, boot loader and the running system.

Update state machine (QOS-FD 3章)::

    confirmed ──update──▶ trial_reserved ──reboot into trial──▶ trial_in_progress
        ▲                      │                                   │
        │                      │ trial never came up               ├─ health OK ──▶ confirmed
        │                      ▼                                   └─ health NG ──▶ rolled_back
        └──────────────── rolled_back  (accepts a new update like confirmed)

The state lives on the state partition and is written atomically
(write temp → fsync → rename → fsync dir).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass

from .config import AdminConfig
from .errors import AdminError

STATE_FILE = "state.json"
LOCK_FILE = "update.lock"

CONFIRMED = "confirmed"
TRIAL_RESERVED = "trial_reserved"
TRIAL_IN_PROGRESS = "trial_in_progress"
ROLLED_BACK = "rolled_back"
STATES = (CONFIRMED, TRIAL_RESERVED, TRIAL_IN_PROGRESS, ROLLED_BACK)


def atomic_write(path: str, data: bytes, mode: int = 0o644) -> None:
    d = os.path.dirname(path) or "."
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


@dataclass
class UpdateState:
    format: int = 1
    state: str = CONFIRMED
    confirmed_slot: str = "a"
    confirmed_version: str = "0.0.0"
    trial_slot: str | None = None
    trial_version: str | None = None
    reserved_boot_id: str | None = None
    last_failure: str | None = None
    updated_at: int = 0


class StateStore:
    def __init__(self, cfg: AdminConfig):
        self.cfg = cfg
        self.path = os.path.join(cfg.state_dir, STATE_FILE)

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> UpdateState:
        try:
            with open(self.path, "rb") as fh:
                data = json.load(fh)
            st = UpdateState(**data)
        except (OSError, ValueError, TypeError) as exc:
            raise AdminError("STATE_CORRUPT", f"cannot read update state: {exc}") from exc
        if st.format != 1 or st.state not in STATES or st.confirmed_slot not in self.cfg.slots:
            raise AdminError("STATE_CORRUPT", "update state has unexpected values")
        return st

    def save(self, st: UpdateState) -> None:
        st.updated_at = int(time.time())
        os.makedirs(self.cfg.state_dir, mode=0o700, exist_ok=True)
        atomic_write(self.path, (json.dumps(asdict(st), indent=2) + "\n").encode(), 0o600)


class Lock:
    """Exclusive, non-blocking update lock (同時更新は受理しない)."""

    def __init__(self, cfg: AdminConfig):
        self.path = os.path.join(cfg.state_dir, LOCK_FILE)
        self.fd: int | None = None

    def __enter__(self):
        import fcntl

        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise AdminError("UPDATE_IN_PROGRESS", "another update operation is running") from exc
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)  # releases the flock


# --------------------------------------------------------------------------
# Running system
# --------------------------------------------------------------------------
_SLOT_RE = re.compile(r"(?:^|\s)quantumos\.slot=([a-z])(?:\s|$)")


def booted_slot(cfg: AdminConfig) -> str:
    try:
        with open(cfg.cmdline_path) as fh:
            cmdline = fh.read()
    except OSError as exc:
        raise AdminError("SLOT_STATE_INVALID", f"cannot read kernel command line: {exc}") from exc
    m = _SLOT_RE.search(cmdline)
    if not m or m.group(1) not in cfg.slots:
        raise AdminError("SLOT_STATE_INVALID", "booted slot unknown (quantumos.slot= missing)")
    return m.group(1)


def boot_id(cfg: AdminConfig) -> str:
    try:
        with open(cfg.boot_id_path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def read_os_version(cfg: AdminConfig) -> str:
    """QUANTUMOS_VERSION= (preferred) or VERSION_ID= from os-release."""
    found = {}
    try:
        with open(cfg.os_release) as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    found[k] = v.strip().strip('"')
    except OSError:
        pass
    return found.get("QUANTUMOS_VERSION") or found.get("VERSION_ID") or "0.0.0"


# --------------------------------------------------------------------------
# Boot loader
# --------------------------------------------------------------------------
def entry_id(slot: str) -> str:
    return f"quantumos-{slot}.conf"


class Bootloader:
    """systemd-boot: one Type #1 entry per slot.  The trial boot is a
    one-shot (LoaderEntryOneShot) so any failure to reach confirmation falls
    back to the default entry, which always names the confirmed slot."""

    def __init__(self, cfg: AdminConfig):
        self.cfg = cfg

    def entry_path(self, slot: str) -> str:
        return os.path.join(self.cfg.entries_dir, entry_id(slot))

    def write_entry(self, slot: str, version: str) -> None:
        s = self.cfg.slots[slot]
        rel = os.path.relpath(s["boot_dir"], self.cfg.esp_dir)
        body = (
            f"title QuantumOS {version} (slot {slot})\n"
            f"version {version}\n"
            f"linux /{rel}/vmlinuz\n"
            f"initrd /{rel}/initrd.img\n"
            f"options root={s['root_spec']} ro quantumos.slot={slot} "
            f"{self.cfg.kernel_cmdline_extra}\n"
        )
        os.makedirs(self.cfg.entries_dir, exist_ok=True)
        atomic_write(self.entry_path(slot), body.encode())

    def remove_entry(self, slot: str) -> None:
        try:
            os.unlink(self.entry_path(slot))
        except FileNotFoundError:
            pass

    def _bootctl(self, *args: str) -> None:
        try:
            subprocess.run(
                ["bootctl", f"--esp-path={self.cfg.esp_dir}", *args],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdminError("BOOTLOADER_FAILED", f"bootctl {' '.join(args)} failed") from exc

    def set_oneshot(self, slot: str | None) -> None:
        self._bootctl("set-oneshot", entry_id(slot) if slot else "")

    def set_default(self, slot: str) -> None:
        self._bootctl("set-default", entry_id(slot))


class FileBootloader(Bootloader):
    """Simulation of the EFI variables in a JSON file (tests, dry runs)."""

    def _load(self) -> dict:
        try:
            with open(self.cfg.bootloader_state_file) as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {"default": None, "oneshot": None}

    def _save(self, data: dict) -> None:
        atomic_write(self.cfg.bootloader_state_file, json.dumps(data).encode())

    def set_oneshot(self, slot: str | None) -> None:
        d = self._load()
        d["oneshot"] = entry_id(slot) if slot else None
        self._save(d)

    def set_default(self, slot: str) -> None:
        d = self._load()
        d["default"] = entry_id(slot)
        self._save(d)

    def next_boot_slot(self) -> str | None:
        """What the firmware would boot next (consumes the one-shot)."""
        d = self._load()
        target = d.get("oneshot") or d.get("default")
        d["oneshot"] = None
        self._save(d)
        if not target:
            return None
        return target[len("quantumos-") : -len(".conf")]


def make_bootloader(cfg: AdminConfig) -> Bootloader:
    if cfg.bootloader == "file":
        if not cfg.bootloader_state_file:
            raise ValueError("bootloader_state_file is required for the file bootloader")
        return FileBootloader(cfg)
    if cfg.bootloader == "systemd-boot":
        return Bootloader(cfg)
    raise ValueError(f"unknown bootloader {cfg.bootloader!r}")
