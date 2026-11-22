"""``qosctl`` – the standard user CLI (QOS-IF 2章).

    qosctl validate FILE
    qosctl run FILE [--shots N] [--seed S]
    qosctl status

On success: exit 0 and the result object as one JSON line on stdout.
On failure: one JSON line ``{"code": ..., "message": ...}`` on stderr, no
partial result, and exit status 2 / 3 / 4 (see ``errors.exit_status_for``).
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys

from . import __version__, errors, protocol, strictjson
from .errors import QosError
from .limits import EXECUTION_TIMEOUT_SEC, MAX_CIRCUIT_BYTES, SHOTS_MAX, SHOTS_MIN
from .seed import parse_seed

# The service deadline plus margin for transport; beyond this the CLI treats
# the response as lost (TRANSPORT_ERROR).
RESPONSE_TIMEOUT_SEC = EXECUTION_TIMEOUT_SEC + 30.0
CONNECT_TIMEOUT_SEC = 5.0

_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z", re.ASCII)


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would print free text and exit 2
        raise QosError(errors.USAGE_INVALID, message)


def _build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="qosctl", description="QuantumOS standard CLI")
    p.add_argument("--version", action="version", version=f"qosctl {__version__}")
    p.add_argument(
        "--socket",
        default=os.environ.get("QOS_SOCKET", protocol.DEFAULT_SOCKET_PATH),
        help=argparse.SUPPRESS,  # for testing; the public socket path is fixed
    )
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    v = sub.add_parser("validate", help="validate a circuit JSON file")
    v.add_argument("file")
    r = sub.add_parser("run", help="run a circuit JSON file")
    r.add_argument("file")
    r.add_argument("--shots")
    r.add_argument("--seed")
    sub.add_parser("status", help="show whether the service is ready or busy")
    return p


def read_circuit_file(path: str) -> str:
    """Read FILE with the caller's own permissions and return its JSON text."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_CIRCUIT_BYTES + 1)
    except OSError as exc:
        raise QosError(errors.FILE_UNREADABLE, f"cannot read {path}: {exc.strerror}") from exc
    if len(data) > MAX_CIRCUIT_BYTES:
        raise QosError(errors.CIRCUIT_INVALID, "circuit file exceeds 4 MiB")
    try:
        text = strictjson.decode_bytes(data)
        value = strictjson.loads(text)
    except strictjson.StrictJSONError as exc:
        raise QosError(errors.CIRCUIT_INVALID, f"malformed circuit JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise QosError(errors.CIRCUIT_INVALID, "circuit must be a JSON object")
    # The text is a single well-formed JSON object, so it can be embedded
    # verbatim; the service then sees the exact bytes the user wrote.
    return text.strip(" \t\r\n")


def build_request(op: str, circuit_text: str | None = None, shots=None, seed=None) -> bytes:
    parts = ['{"protocol_version":1,"op":', strictjson.dumps(op)]
    if circuit_text is not None:
        parts += [',"circuit":', circuit_text]
    if shots is not None:
        parts += [',"shots":', str(shots)]
    if seed is not None:
        parts += [',"seed":', strictjson.dumps(seed)]
    parts.append("}")
    return "".join(parts).encode("utf-8")


def call_service(socket_path: str, request: bytes) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(CONNECT_TIMEOUT_SEC)
        try:
            sock.connect(socket_path)
        except PermissionError as exc:
            raise QosError(errors.ACCESS_DENIED, "permission denied on service socket") from exc
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
            raise QosError(errors.SERVICE_UNAVAILABLE, "cannot connect to quantum service") from exc
        try:
            sock.settimeout(RESPONSE_TIMEOUT_SEC)
            protocol.write_frame(sock, request)
            body = protocol.read_frame(sock, timeout=RESPONSE_TIMEOUT_SEC)
        except (OSError, protocol.FrameError) as exc:
            raise QosError(errors.TRANSPORT_ERROR, "connection lost or response invalid") from exc
    finally:
        sock.close()
    resp = protocol.parse_response(body)
    if not resp["ok"]:
        err = resp["error"]
        raise QosError(err["code"], err["message"])
    return resp["result"]


def _parse_shots(value: str) -> int:
    if not _DECIMAL_RE.match(value) or not (SHOTS_MIN <= int(value) <= SHOTS_MAX):
        raise QosError(
            errors.RUN_OPTION_INVALID, f"--shots must be an integer in {SHOTS_MIN}..{SHOTS_MAX}"
        )
    return int(value)


def _run(argv: list[str] | None) -> dict:
    args = _build_parser().parse_args(argv)
    if args.command == "status":
        return call_service(args.socket, build_request("status"))
    if args.command == "validate":
        text = read_circuit_file(args.file)
        return call_service(args.socket, build_request("validate", text))
    # run: check options before contacting the service (exit 2 locally)
    shots = _parse_shots(args.shots) if args.shots is not None else None
    if args.seed is not None:
        try:
            parse_seed(args.seed)
        except ValueError as exc:
            raise QosError(errors.RUN_OPTION_INVALID, f"Invalid seed: {exc}") from exc
    text = read_circuit_file(args.file)
    return call_service(args.socket, build_request("run", text, shots, args.seed))


def main(argv: list[str] | None = None) -> int:
    try:
        result = _run(argv)
    except QosError as exc:
        sys.stderr.write(strictjson.dumps(exc.to_dict()) + "\n")
        return errors.exit_status_for(exc.code)
    except KeyboardInterrupt:
        sys.stderr.write(
            strictjson.dumps({"code": errors.TRANSPORT_ERROR, "message": "interrupted"}) + "\n"
        )
        return errors.EXIT_EXECUTION
    sys.stdout.write(strictjson.dumps(result) + "\n")
    return errors.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
