"""Logic of the QuantumOS update manager (QOS-DT 3.2).

The manager runs as the desktop user and reaches the root-only ``qos-admin``
through ``pkexec`` and the fixed helper ``/usr/libexec/quantumos/qos-update-helper``
(polkit actions ``org.quantumos.update.status`` / ``org.quantumos.update.apply``).
It reads the provisional ``--json`` output of ``qos-admin``.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

PKEXEC = "/usr/bin/pkexec"
HELPER = "/usr/libexec/quantumos/qos-update-helper"
QOS_ADMIN = "/usr/bin/qos-admin"
HELPER_TIMEOUT_SEC = 3600.0  # writing a slot can take a while on slow disks

# pkexec(1): 126 = authorization could not be obtained (dialog dismissed),
# 127 = not authorized / authentication failed.
PKEXEC_DISMISSED = 126
PKEXEC_NOT_AUTHORIZED = 127

STATE_TEXT = {
    "confirmed": "確定済み（通常の状態）",
    "trial_reserved": "更新を書き込み済み。再起動すると新しい版を試行起動します",
    "trial_in_progress": "新しい版を試行起動中です（健全性確認の完了待ち）",
    "rolled_back": "前回の更新は失敗したため、元の版に戻しました",
}

FAILURE_TEXT = {
    "PERMISSION_DENIED": "管理者権限が必要です。",
    "UPDATE_IN_PROGRESS": "別の更新処理が実行中です。終わるまで待ってください。",
    "UPDATE_PENDING": "書き込み済みの更新があります。再起動して試行起動を完了してください。",
    "BUNDLE_INVALID": "更新バンドルの形式が不正です。",
    "KEY_UNAVAILABLE": "更新の検証鍵がこの OS にありません。",
    "SIGNATURE_INVALID": "更新バンドルの署名を検証できません。配布元を確認してください。",
    "MANIFEST_INVALID": "更新バンドルの内容一覧が不正です。",
    "PRODUCT_MISMATCH": "この OS（版）向けの更新バンドルではありません。",
    "ARCH_MISMATCH": "この機械のアーキテクチャー向けの更新バンドルではありません。",
    "VERSION_REJECTED": "現在の版と同じか古い版の更新は適用できません。",
    "HASH_MISMATCH": "更新バンドルが壊れています（ハッシュ不一致）。取得し直してください。",
    "INSUFFICIENT_SPACE": "更新先の領域が不足しています。",
    "SLOT_STATE_INVALID": "更新先スロットの状態が不正です。",
    "WRITE_FAILED": "更新先スロットへの書き込みに失敗しました。",
    "VERIFY_FAILED": "書き込んだ内容の検証に失敗しました。",
    "BOOTLOADER_FAILED": "起動設定を更新できませんでした。",
    "STATE_CORRUPT": "更新状態の記録が壊れています。保守手順に従って復旧してください。",
    "TRIAL_BOOT_FAILED": "新しい版の試行起動が完了しませんでした。",
    "HEALTH_CHECK_FAILED": "新しい版の健全性確認に失敗しました。",
    "HEALTH_CHECK_TIMEOUT": "新しい版の健全性確認が時間内に終わりませんでした。",
    "HEALTH_CHECK_INCOMPLETE": "新しい版の健全性確認が完了しないまま再起動されました。",
    "CONFIG_INVALID": "更新ツールの設定が不正です。",
    "HELPER_FAILED": "更新ツールを実行できませんでした。",
}


def describe_failure(code: str | None) -> str:
    if not code:
        return "なし"
    return f"{FAILURE_TEXT.get(code, '不明な失敗です。')}（{code}）"


@dataclass(frozen=True)
class UpdateStatus:
    confirmed_slot: str | None
    confirmed_version: str | None
    trial_slot: str | None
    trial_version: str | None
    state: str
    last_failure: str | None
    booted_slot: str | None

    @property
    def state_text(self) -> str:
        return STATE_TEXT.get(self.state, self.state)

    @property
    def needs_reboot(self) -> bool:
        return self.state == "trial_reserved"

    @property
    def can_apply(self) -> bool:
        return self.state in ("confirmed", "rolled_back")


@dataclass(frozen=True)
class AdminFailure:
    code: str
    message: str

    @property
    def text(self) -> str:
        return describe_failure(self.code)


def _opt_str(obj: dict, key: str) -> str | None:
    v = obj.get(key)
    if v is not None and not isinstance(v, str):
        raise ValueError(f"{key} が不正です")
    return v


def parse_status(stdout: str) -> UpdateStatus:
    obj = json.loads(stdout.strip().splitlines()[-1])
    if not isinstance(obj, dict) or not isinstance(obj.get("state"), str):
        raise ValueError("更新状態を解釈できません")
    return UpdateStatus(
        confirmed_slot=_opt_str(obj, "confirmed_slot"),
        confirmed_version=_opt_str(obj, "confirmed_version"),
        trial_slot=_opt_str(obj, "trial_slot"),
        trial_version=_opt_str(obj, "trial_version"),
        state=obj["state"],
        last_failure=_opt_str(obj, "last_failure"),
        booted_slot=_opt_str(obj, "booted_slot"),
    )


def parse_error(stderr: str) -> AdminFailure:
    for line in reversed(stderr.strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict) and isinstance(err.get("code"), str):
            return AdminFailure(err["code"], str(err.get("message", "")))
    text = stderr.strip()
    if text.startswith("error: ") and ":" in text[7:]:
        code, _, msg = text[7:].partition(":")
        return AdminFailure(code.strip(), msg.strip())
    return AdminFailure("HELPER_FAILED", text[-500:])


def status_argv() -> list[str]:
    return [PKEXEC, HELPER, "status"]


def apply_argv(bundle: str) -> list[str]:
    return [PKEXEC, HELPER, "apply", os.path.abspath(bundle)]


def interpret(argv_kind: str, status: int, out: str, err: str):
    """Return UpdateStatus (or None for a dismissed dialog) or AdminFailure."""
    if status == PKEXEC_DISMISSED:
        return None
    if status == PKEXEC_NOT_AUTHORIZED:
        return AdminFailure("PERMISSION_DENIED", "認証できませんでした")
    if status != 0:
        return parse_error(err)
    try:
        return parse_status(out)
    except (ValueError, IndexError) as exc:
        return AdminFailure("HELPER_FAILED", f"出力を解釈できません: {exc}")


def run(argv: list[str], kind: str, runner=subprocess.run):
    try:
        p = runner(argv, capture_output=True, text=True, timeout=HELPER_TIMEOUT_SEC,
                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return AdminFailure("HELPER_FAILED", str(exc))
    return interpret(kind, p.returncode, p.stdout, p.stderr)


def helper_main(argv: list[str], execv=os.execv) -> int:
    """Entry point of qos-update-helper (runs as root through pkexec).

    Accepts only ``status`` and ``apply ABSOLUTE_BUNDLE_PATH`` so the polkit
    actions can be told apart by their first argument.
    """
    import sys

    if argv == ["status"]:
        execv(QOS_ADMIN, [QOS_ADMIN, "--json", "update-status"])
        return 0
    if len(argv) == 2 and argv[0] == "apply":
        bundle = argv[1]
        if not os.path.isabs(bundle) or not os.path.isfile(bundle):
            print(json.dumps({"error": {"code": "BUNDLE_INVALID",
                                        "message": "bundle must be an existing absolute path"}}),
                  file=sys.stderr)
            return 1
        execv(QOS_ADMIN, [QOS_ADMIN, "--json", "update", bundle])
        return 0
    print(json.dumps({"error": {"code": "HELPER_FAILED", "message": "usage: status | apply BUNDLE"}}),
          file=sys.stderr)
    return 2
