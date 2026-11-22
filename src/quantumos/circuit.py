"""Circuit JSON version 1 validation (QOS-FD OS-03, QOS-IF 3章).

Structure::

    {"schema_version": 1, "n_qubits": N, "operations": [
        {"op": "h", "qubits": [q]},
        {"op": "rx", "qubits": [q], "theta": 0.5},
        {"op": "cx", "qubits": [control, target]},
        {"op": "measure", "qubits": [q0, q1, ...]}]}

Rules applied here (interpretation of v0.1 documents, see
docs/implementation-notes.md):

* ``schema_version`` must be an integer; any integer other than 1 is
  ``SCHEMA_UNSUPPORTED``, a non-integer is ``CIRCUIT_INVALID``.
* 1 ≤ ``n_qubits`` ≤ 64 (structural), 1 … 10,000 operations incl. measure.
* gates h/x/z/rx/rz take exactly one qubit, cx takes two distinct qubits.
* rx/rz require a finite radian ``theta``; no other op may carry ``theta``.
* exactly one ``measure`` operation, which must be the last operation, over
  one or more distinct qubits (terminal measurement).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import errors
from .errors import QosError
from .limits import MAX_OPERATIONS, STRUCT_MAX_QUBITS, STRUCT_MIN_QUBITS

SINGLE_QUBIT_GATES = frozenset({"h", "x", "z"})
ROTATION_GATES = frozenset({"rx", "rz"})
TWO_QUBIT_GATES = frozenset({"cx"})
MEASURE = "measure"
ALL_OPS = SINGLE_QUBIT_GATES | ROTATION_GATES | TWO_QUBIT_GATES | {MEASURE}

_ROOT_KEYS = frozenset({"schema_version", "n_qubits", "operations"})


@dataclass(frozen=True)
class Gate:
    name: str
    qubits: tuple[int, ...]
    theta: float | None = None


@dataclass(frozen=True)
class Circuit:
    n_qubits: int
    gates: tuple[Gate, ...]  # unitary gates, in order
    measured_qubits: tuple[int, ...]


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _invalid(msg: str) -> QosError:
    return QosError(errors.CIRCUIT_INVALID, msg)


def validate_circuit(obj: object) -> Circuit:
    """Validate a decoded circuit JSON object; raise ``QosError`` on failure."""
    if not isinstance(obj, dict):
        raise _invalid("circuit must be a JSON object")

    # schema version is checked first so that a future version with a
    # different layout is reported as unsupported rather than invalid.
    if "schema_version" not in obj:
        raise _invalid("missing key: schema_version")
    sv = obj["schema_version"]
    if not _is_int(sv):
        raise _invalid("schema_version must be an integer")
    if sv != 1:
        raise QosError(errors.SCHEMA_UNSUPPORTED, f"unsupported schema_version: {sv}")

    for key in obj:
        if key not in _ROOT_KEYS:
            raise _invalid(f"unknown key: {key}")
    for key in ("n_qubits", "operations"):
        if key not in obj:
            raise _invalid(f"missing key: {key}")

    n = obj["n_qubits"]
    if not _is_int(n) or not (STRUCT_MIN_QUBITS <= n <= STRUCT_MAX_QUBITS):
        raise _invalid(
            f"n_qubits must be an integer in {STRUCT_MIN_QUBITS}..{STRUCT_MAX_QUBITS}"
        )

    ops = obj["operations"]
    if not isinstance(ops, list):
        raise _invalid("operations must be an array")
    if not ops:
        raise _invalid("operations must not be empty")
    if len(ops) > MAX_OPERATIONS:
        raise _invalid(f"too many operations (max {MAX_OPERATIONS})")

    gates: list[Gate] = []
    measured: tuple[int, ...] | None = None
    last = len(ops) - 1
    for i, op in enumerate(ops):
        where = f"operations[{i}]"
        if not isinstance(op, dict):
            raise _invalid(f"{where} must be an object")
        name = op.get("op")
        if not isinstance(name, str):
            raise _invalid(f"{where}.op must be a string")
        if name not in ALL_OPS:
            raise _invalid(f"{where}: unknown op: {name}")
        allowed = {"op", "qubits"} | ({"theta"} if name in ROTATION_GATES else set())
        for key in op:
            if key not in allowed:
                raise _invalid(f"{where}: unknown key: {key}")
        if "qubits" not in op:
            raise _invalid(f"{where}: missing key: qubits")
        qubits = op["qubits"]
        if not isinstance(qubits, list):
            raise _invalid(f"{where}.qubits must be an array")
        for q in qubits:
            if not _is_int(q) or not (0 <= q < n):
                raise _invalid(f"{where}.qubits: index out of range 0..{n - 1}")
        if len(set(qubits)) != len(qubits):
            raise _invalid(f"{where}.qubits: duplicate qubit")

        if name == MEASURE:
            if i != last:
                raise _invalid(f"{where}: measure must be the final operation")
            if not qubits:
                raise _invalid(f"{where}: measure needs at least one qubit")
            measured = tuple(qubits)
            continue

        arity = 2 if name in TWO_QUBIT_GATES else 1
        if len(qubits) != arity:
            raise _invalid(f"{where}: {name} takes exactly {arity} qubit(s)")
        theta = None
        if name in ROTATION_GATES:
            if "theta" not in op:
                raise _invalid(f"{where}: missing key: theta")
            theta = op["theta"]
            if isinstance(theta, bool) or not isinstance(theta, (int, float)):
                raise _invalid(f"{where}.theta must be a number")
            theta = float(theta)
            if not math.isfinite(theta):
                raise _invalid(f"{where}.theta must be finite")
        gates.append(Gate(name, tuple(qubits), theta))

    if measured is None:
        raise _invalid("terminal measure operation is required")
    return Circuit(n, tuple(gates), measured)
