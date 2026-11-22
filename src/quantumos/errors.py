"""Error codes and CLI exit-status mapping (QOS-IF 2章・4章)."""

from __future__ import annotations

# --- IPC / service error codes (QOS-IF 4章) ---------------------------------
PROTOCOL_INVALID = "PROTOCOL_INVALID"
PROTOCOL_UNSUPPORTED = "PROTOCOL_UNSUPPORTED"
CIRCUIT_INVALID = "CIRCUIT_INVALID"
SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
RUN_OPTION_INVALID = "RUN_OPTION_INVALID"
BACKEND_LIMIT = "BACKEND_LIMIT"
ACCESS_DENIED = "ACCESS_DENIED"
BUSY = "BUSY"
TIMEOUT = "TIMEOUT"
EXECUTION_FAILED = "EXECUTION_FAILED"
# CLI-side only
SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
# CLI-local provisional codes (not in QOS-IF 0.1; see docs/implementation-notes.md)
USAGE_INVALID = "USAGE_INVALID"
FILE_UNREADABLE = "FILE_UNREADABLE"

SERVICE_CODES = frozenset(
    {
        PROTOCOL_INVALID,
        PROTOCOL_UNSUPPORTED,
        CIRCUIT_INVALID,
        SCHEMA_UNSUPPORTED,
        RUN_OPTION_INVALID,
        BACKEND_LIMIT,
        ACCESS_DENIED,
        BUSY,
        TIMEOUT,
        EXECUTION_FAILED,
    }
)

EXIT_OK = 0
EXIT_INPUT = 2  # CLI引数、回路、形式、版、実行オプション、実行上限の違反
EXIT_ACCESS = 3  # 接続不能、権限拒否、利用可能な計算枠なし
EXIT_EXECUTION = 4  # 計算失敗、期限超過、途中切断または応答不正

_EXIT_BY_CODE = {
    USAGE_INVALID: EXIT_INPUT,
    FILE_UNREADABLE: EXIT_INPUT,
    PROTOCOL_INVALID: EXIT_INPUT,
    PROTOCOL_UNSUPPORTED: EXIT_INPUT,
    CIRCUIT_INVALID: EXIT_INPUT,
    SCHEMA_UNSUPPORTED: EXIT_INPUT,
    RUN_OPTION_INVALID: EXIT_INPUT,
    BACKEND_LIMIT: EXIT_INPUT,
    SERVICE_UNAVAILABLE: EXIT_ACCESS,
    ACCESS_DENIED: EXIT_ACCESS,
    BUSY: EXIT_ACCESS,
    TIMEOUT: EXIT_EXECUTION,
    EXECUTION_FAILED: EXIT_EXECUTION,
    TRANSPORT_ERROR: EXIT_EXECUTION,
}


def exit_status_for(code: str) -> int:
    """Return the qosctl exit status for an error code (unknown → 4)."""
    return _EXIT_BY_CODE.get(code, EXIT_EXECUTION)


class QosError(Exception):
    """An error that maps onto a protocol error object ``{code, message}``."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}
