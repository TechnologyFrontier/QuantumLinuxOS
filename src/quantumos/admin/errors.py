"""Failure codes of the maintenance tooling (provisional, QOS-IF 5章)."""


class AdminError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# Codes used by qos-admin (documented in docs/update-and-recovery.md):
#   PERMISSION_DENIED, UPDATE_IN_PROGRESS, UPDATE_PENDING, BUNDLE_INVALID,
#   KEY_UNAVAILABLE, SIGNATURE_INVALID, MANIFEST_INVALID, PRODUCT_MISMATCH,
#   ARCH_MISMATCH, VERSION_REJECTED, HASH_MISMATCH, INSUFFICIENT_SPACE,
#   SLOT_STATE_INVALID, WRITE_FAILED, VERIFY_FAILED, BOOTLOADER_FAILED,
#   STATE_CORRUPT,
# and recorded as last_failure by boot assessment:
#   TRIAL_BOOT_FAILED, HEALTH_CHECK_FAILED, HEALTH_CHECK_TIMEOUT,
#   HEALTH_CHECK_INCOMPLETE
