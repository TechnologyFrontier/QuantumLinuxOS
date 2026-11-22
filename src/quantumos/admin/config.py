"""Configuration of the update / maintenance tooling (QOS-FD OS-06/07).

The production defaults describe the disk layout of QOS-BD 3章:

* ESP (mounted at /efi) holding systemd-boot, one loader entry and one
  kernel/initrd directory per root slot,
* root partitions ``qos-root-a`` / ``qos-root-b`` (read-only while booted),
* the state partition, mounted at /var, holding /var/lib/quantumos/update.

Every path can be overridden by a JSON file named by ``$QOS_ADMIN_CONFIG``
so the whole update flow can be exercised against plain files in tests.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

DEFAULT_CONFIG_PATH = "/usr/lib/quantumos/update.conf"


def _default_slots() -> dict:
    return {
        s: {
            "root_device": f"/dev/disk/by-partlabel/qos-root-{s}",
            "root_spec": f"PARTLABEL=qos-root-{s}",
            "boot_dir": f"/efi/quantumos/{s}",
        }
        for s in ("a", "b")
    }


def _default_health_checks() -> list:
    return [
        {"name": "root-readonly", "builtin": "root_readonly"},
        {"name": "state-mounted", "builtin": "state_writable"},
        {"name": "console", "argv": ["systemctl", "is-active", "--quiet", "getty@tty1.service"]},
        {"name": "qosd-active", "argv": ["systemctl", "is-active", "--quiet", "qosd.service"]},
        {"name": "qosd-socket", "builtin": "socket_probe"},
        {"name": "qosd-self-test", "argv": ["/usr/bin/qosd", "--self-test"]},
    ]


@dataclass
class AdminConfig:
    product_id: str = "quantumos"
    architecture: str = "x86_64"
    slots: dict = field(default_factory=_default_slots)
    state_dir: str = "/var/lib/quantumos/update"
    public_key: str = "/usr/share/quantumos/keys/update-ed25519.pub"
    os_release: str = "/usr/lib/os-release"
    bootloader: str = "systemd-boot"  # or "file" (simulation / tests)
    esp_dir: str = "/efi"
    entries_dir: str = "/efi/loader/entries"
    bootloader_state_file: str | None = None  # used by the "file" bootloader
    kernel_cmdline_extra: str = "quiet"
    cmdline_path: str = "/proc/cmdline"
    boot_id_path: str = "/proc/sys/kernel/random/boot_id"
    mounts_path: str = "/proc/self/mounts"
    socket_path: str = "/run/quantumos/quantum.sock"
    health_checks: list = field(default_factory=_default_health_checks)
    health_check_timeout: float = 60.0  # per check
    health_deadline: float = 300.0  # whole assessment (provisional)
    require_root: bool = True

    @classmethod
    def load(cls, path: str | None = None) -> "AdminConfig":
        path = path or os.environ.get("QOS_ADMIN_CONFIG") or DEFAULT_CONFIG_PATH
        cfg = cls()
        if os.path.exists(path):
            with open(path, "rb") as fh:
                data = json.load(fh)
            for key, value in data.items():
                if not hasattr(cfg, key):
                    raise ValueError(f"unknown config key: {key}")
                setattr(cfg, key, value)
        return cfg

    def other_slot(self, slot: str) -> str:
        names = sorted(self.slots)
        if slot not in names or len(names) != 2:
            raise ValueError(f"unknown slot {slot!r}")
        return names[1] if slot == names[0] else names[0]
