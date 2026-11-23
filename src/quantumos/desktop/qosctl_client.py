"""Calling the standard CLI ``qosctl`` from the desktop apps (QOS-DT 3.1).

The apps never connect to the service socket themselves: they start
``/usr/bin/qosctl`` with individual arguments (no shell) and interpret the
contract of QOS-IF 2章 — exit 0 with one result JSON line on stdout, or
exit 2/3/4 with one error JSON line on stderr.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field

from .. import strictjson
from ..cli import RESPONSE_TIMEOUT_SEC

DEFAULT_QOSCTL = "/usr/bin/qosctl"
# the CLI itself gives up after RESPONSE_TIMEOUT_SEC; this is a safety net
PROCESS_TIMEOUT_SEC = RESPONSE_TIMEOUT_SEC + 30.0
MAX_OUTPUT_BYTES = 16 * 1024 * 1024

# Japanese guidance per error code (QIDE-FD 3.1 と同じ区分).
GUIDANCE = {
    "CIRCUIT_INVALID": "回路 JSON の構造を確認してください。",
    "SCHEMA_UNSUPPORTED": "schema_version は 1 にしてください。",
    "RUN_OPTION_INVALID": "観測回数（1〜100,000）またはシードを確認してください。",
    "BACKEND_LIMIT": "この実行環境の上限（20 量子ビット）を超えています。回路を小さくしてください。",
    "ACCESS_DENIED": "量子サービスを使う権限がありません。管理者に quantum グループへの追加を依頼し、ログインし直してください。",
    "BUSY": "量子サービスは別の計算を実行中です。終わってから実行し直してください（自動では再試行しません）。",
    "SERVICE_UNAVAILABLE": "量子サービスに接続できません。更新マネージャーまたは「qos-admin service-status」でサービスの状態を確認してください。",
    "TIMEOUT": "実行期限（120 秒）を超えました。回路の規模や観測回数を減らしてください。",
    "EXECUTION_FAILED": "計算に失敗しました。もう一度実行しても失敗する場合は管理者に連絡してください。",
    "TRANSPORT_ERROR": "量子サービスとの通信が途中で切れました。もう一度実行してください。",
    "FILE_UNREADABLE": "回路ファイルを読み込めません。",
    "USAGE_INVALID": "qosctl の呼び出しが不正です（アプリの不具合の可能性があります）。",
    "PROTOCOL_INVALID": "要求の形式が不正です（アプリの不具合の可能性があります）。",
    "PROTOCOL_UNSUPPORTED": "量子サービスの版が対応していません。OS を更新してください。",
}

CATEGORY = {2: "入力の誤り", 3: "利用できない", 4: "実行の失敗"}


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    exit_status: int | None = None

    @property
    def guidance(self) -> str:
        return GUIDANCE.get(self.code, "予期しないエラーです。")

    @property
    def category(self) -> str:
        return CATEGORY.get(self.exit_status or 0, "不明")


@dataclass(frozen=True)
class RunResult:
    counts: tuple[tuple[str, int], ...]
    shots: int
    seed: str
    measured_qubits: tuple[int, ...]
    backend_id: str = ""
    service_version: str = ""
    raw_line: str = field(default="", compare=False, repr=False)

    def rows(self) -> list[tuple[str, int, float]]:
        """(key, count, ratio) sorted by key, as shown in the results table."""
        total = sum(n for _, n in self.counts) or 1
        return [(k, n, n / total) for k, n in sorted(self.counts)]


class ResponseError(ValueError):
    pass


def _protocol_failure(detail: str, status: int | None) -> Failure:
    return Failure("TRANSPORT_ERROR", f"qosctl の出力を解釈できません: {detail}", status)


def parse_failure(stderr: bytes, status: int) -> Failure:
    lines = [ln for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    if len(lines) != 1:
        return _protocol_failure("標準エラーが 1 行の JSON ではありません", status)
    try:
        obj = strictjson.loads(lines[0])
    except strictjson.StrictJSONError:
        return _protocol_failure("標準エラーが JSON ではありません", status)
    if not (isinstance(obj, dict) and isinstance(obj.get("code"), str)
            and isinstance(obj.get("message"), str)):
        return _protocol_failure("エラー JSON に code/message がありません", status)
    return Failure(obj["code"], obj["message"], status)


def _single_line(stdout: bytes) -> str:
    lines = [ln for ln in stdout.decode("utf-8").splitlines() if ln.strip()]
    if len(lines) != 1:
        raise ResponseError("標準出力が 1 行の JSON ではありません")
    return lines[0]


def parse_run_result(stdout: bytes) -> RunResult:
    line = _single_line(stdout)
    try:
        obj = strictjson.loads(line)
    except strictjson.StrictJSONError as exc:
        raise ResponseError(f"結果が JSON ではありません: {exc}") from exc
    if not isinstance(obj, dict):
        raise ResponseError("結果がオブジェクトではありません")
    counts = obj.get("counts")
    shots = obj.get("shots")
    seed = obj.get("seed")
    measured = obj.get("measured_qubits")
    if not isinstance(counts, list) or not all(
        isinstance(p, list) and len(p) == 2 and isinstance(p[0], str)
        and isinstance(p[1], int) and not isinstance(p[1], bool) and p[1] >= 0
        for p in counts
    ):
        raise ResponseError("counts が不正です")
    if not isinstance(shots, int) or isinstance(shots, bool):
        raise ResponseError("shots が不正です")
    if not isinstance(seed, str):
        raise ResponseError("seed が文字列ではありません")
    if not isinstance(measured, list) or not all(isinstance(q, int) for q in measured):
        raise ResponseError("measured_qubits が不正です")
    if sum(p[1] for p in counts) != shots:
        raise ResponseError("観測回数の合計が shots と一致しません")
    width = len(measured)
    if any(len(p[0]) != width or set(p[0]) - {"0", "1"} for p in counts):
        raise ResponseError("観測キーの形式が不正です")
    return RunResult(
        counts=tuple((p[0], p[1]) for p in counts),
        shots=shots,
        seed=seed,
        measured_qubits=tuple(measured),
        backend_id=str(obj.get("backend_id", "")),
        service_version=str(obj.get("service_version", "")),
        raw_line=line,
    )


def parse_status(stdout: bytes) -> str:
    obj = strictjson.loads(_single_line(stdout))
    state = obj.get("state") if isinstance(obj, dict) else None
    if not isinstance(state, str):
        raise ResponseError("state がありません")
    return state


def run_argv(qosctl: str, circuit_path: str, shots: int | None, seed: str | None) -> list[str]:
    argv = [qosctl, "run", circuit_path]
    if shots is not None:
        argv += ["--shots", str(shots)]
    if seed is not None:
        argv += ["--seed", seed]
    return argv


class Invocation:
    """One qosctl child process that can be stopped from the UI thread.

    ``wait()`` returns ``(status, stdout, stderr)``; after ``stop()`` the
    output is discarded by the caller (a result that arrives after a stop is
    never adopted).
    """

    def __init__(self, argv: list[str], env: dict | None = None):
        self.argv = argv
        self.stopped = False
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

    def wait(self, timeout: float = PROCESS_TIMEOUT_SEC) -> tuple[int, bytes, bytes]:
        try:
            out, err = self._proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            out, err = self._proc.communicate()
            return -1, b"", b""
        if len(out) > MAX_OUTPUT_BYTES or len(err) > MAX_OUTPUT_BYTES:
            return -1, b"", b""
        return self._proc.returncode, out, err

    def stop(self) -> None:
        with self._lock:
            self.stopped = True
            if self._proc.poll() is None:
                self._proc.kill()


class QosctlClient:
    def __init__(self, qosctl: str | None = None, env: dict | None = None):
        self.qosctl = qosctl or os.environ.get("QOS_DESKTOP_QOSCTL", DEFAULT_QOSCTL)
        self.env = env

    def start(self, argv_tail: list[str]) -> Invocation:
        return Invocation([self.qosctl, *argv_tail], self.env)

    def start_run(self, circuit_path: str, shots: int | None, seed: str | None) -> Invocation:
        return Invocation(run_argv(self.qosctl, circuit_path, shots, seed), self.env)

    @staticmethod
    def interpret(inv: Invocation, status: int, out: bytes, err: bytes, kind: str):
        """Return a RunResult/str (status) /True (validate) or a Failure."""
        if status == 0:
            try:
                if kind == "run":
                    return parse_run_result(out)
                if kind == "status":
                    return parse_status(out)
                _single_line(out)
                return True
            except (ResponseError, strictjson.StrictJSONError, UnicodeDecodeError) as exc:
                return _protocol_failure(str(exc), status)
        if status in (2, 3, 4):
            return parse_failure(err, status)
        if status == -1:
            return Failure("TRANSPORT_ERROR", "qosctl が応答しないため停止しました", None)
        return _protocol_failure(f"想定外の終了値 {status}", status)

    def call(self, argv_tail: list[str], kind: str):
        try:
            inv = self.start(argv_tail)
        except OSError as exc:
            return Failure("SERVICE_UNAVAILABLE", f"qosctl を起動できません: {exc.strerror}", None)
        return self.interpret(inv, *inv.wait(), kind)

    def status(self):
        return self.call(["status"], "status")

    def validate(self, path: str):
        return self.call(["validate", path], "validate")
