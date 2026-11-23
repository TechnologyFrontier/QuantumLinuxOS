"""Editable circuit model of the quantum circuit studio (QOS-DT 3.1).

The model is kept in the circuit JSON v1 vocabulary.  Local checks reuse the
service's own validator (``quantumos.circuit``) so the studio reports the same
problems the service would; the service still makes the final decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from .. import strictjson
from ..circuit import ALL_OPS, MEASURE, ROTATION_GATES, TWO_QUBIT_GATES, validate_circuit
from ..errors import QosError
from ..limits import BACKEND_MAX_QUBITS, MAX_CIRCUIT_BYTES, STRUCT_MAX_QUBITS

GATE_LABELS = {
    "h": "H（アダマール）",
    "x": "X（ビット反転）",
    "z": "Z（位相反転）",
    "rx": "RX(θ)（X 軸回転）",
    "rz": "RZ(θ)（Z 軸回転）",
    "cx": "CX（制御 NOT）",
}
EDITABLE_GATES = tuple(GATE_LABELS)  # measure is managed separately


@dataclass(frozen=True)
class Operation:
    op: str
    qubits: tuple[int, ...]
    theta: float | None = None

    def to_json(self) -> dict:
        d: dict = {"op": self.op, "qubits": list(self.qubits)}
        if self.op in ROTATION_GATES:
            d["theta"] = self.theta if self.theta is not None else 0.0
        return d

    def describe(self) -> str:
        qs = ", ".join(f"q{q}" for q in self.qubits)
        if self.op in ROTATION_GATES:
            return f"{self.op.upper()}({format_theta(self.theta or 0.0)})  {qs}"
        if self.op in TWO_QUBIT_GATES:
            return f"CX  制御 q{self.qubits[0]} → 標的 q{self.qubits[1]}"
        return f"{self.op.upper()}  {qs}"


def format_theta(theta: float) -> str:
    for num, den, text in ((1, 1, "π"), (1, 2, "π/2"), (1, 4, "π/4"), (-1, 2, "-π/2"),
                           (-1, 4, "-π/4"), (-1, 1, "-π")):
        if math.isclose(theta, math.pi * num / den, abs_tol=1e-12):
            return text
    return f"{theta:g}"


def parse_theta(text: str) -> float:
    """Accept a plain number or a multiple of π such as ``pi/2``, ``-π/4``, ``0.5pi``."""
    t = text.strip().replace("π", "pi").replace(" ", "")
    if not t:
        raise ValueError("角度を入力してください")
    sign = 1.0
    if t[0] in "+-":
        sign = -1.0 if t[0] == "-" else 1.0
        t = t[1:]
    if "pi" in t:
        num, _, den = t.partition("/")
        coef = num.replace("*", "").replace("pi", "", 1)
        if "pi" in coef or "pi" in den:
            raise ValueError("角度の形式が不正です")
        value = (float(coef) if coef else 1.0) * math.pi
        if den:
            value /= float(den)
    else:
        value = float(t)
    value *= sign
    if not math.isfinite(value):
        raise ValueError("角度は有限の値にしてください")
    return value


@dataclass
class CircuitModel:
    n_qubits: int = 2
    operations: list[Operation] = field(default_factory=list)
    measured: list[int] = field(default_factory=lambda: [0, 1])

    # --- editing --------------------------------------------------------
    def set_qubits(self, n: int) -> None:
        if not 1 <= n <= STRUCT_MAX_QUBITS:
            raise ValueError(f"量子ビット数は 1〜{STRUCT_MAX_QUBITS} です")
        grow = n > self.n_qubits
        old = self.n_qubits
        self.n_qubits = n
        self.operations = [o for o in self.operations if all(q < n for q in o.qubits)]
        self.measured = [q for q in self.measured if q < n]
        if grow and self.measured == list(range(old)):
            self.measured = list(range(n))

    def add(self, op: str, qubits: tuple[int, ...], theta: float | None = None,
            index: int | None = None) -> Operation:
        if op not in EDITABLE_GATES:
            raise ValueError(f"未対応のゲートです: {op}")
        o = Operation(op, tuple(qubits), theta if op in ROTATION_GATES else None)
        self.operations.insert(len(self.operations) if index is None else index, o)
        return o

    def remove(self, index: int) -> None:
        del self.operations[index]

    def move(self, index: int, delta: int) -> int:
        j = index + delta
        if not (0 <= index < len(self.operations) and 0 <= j < len(self.operations)):
            return index
        ops = self.operations
        ops[index], ops[j] = ops[j], ops[index]
        return j

    def update(self, index: int, **changes) -> None:
        self.operations[index] = replace(self.operations[index], **changes)

    # --- JSON -----------------------------------------------------------
    def to_json_obj(self) -> dict:
        ops = [o.to_json() for o in self.operations]
        ops.append({"op": MEASURE, "qubits": list(self.measured)})
        return {"schema_version": 1, "n_qubits": self.n_qubits, "operations": ops}

    def to_json_text(self) -> str:
        """Readable circuit JSON: one operation per line, like examples/bell.json."""
        obj = self.to_json_obj()
        lines = ",\n".join("  " + strictjson.dumps(o) for o in obj["operations"])
        return (f'{{"schema_version":1,"n_qubits":{self.n_qubits},"operations":[\n'
                f"{lines}]}}\n")

    @classmethod
    def from_json_text(cls, text: str | bytes) -> "CircuitModel":
        """Load a circuit for editing; raises ValueError if it cannot be edited."""
        if isinstance(text, str):
            text = text.encode("utf-8")
        if len(text) > MAX_CIRCUIT_BYTES:
            raise ValueError("回路ファイルが 4 MiB を超えています")
        try:
            obj = strictjson.loads(strictjson.decode_bytes(text))
        except strictjson.StrictJSONError as exc:
            raise ValueError(f"JSON として読み込めません: {exc}") from exc
        try:
            validate_circuit(obj)
        except QosError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc
        model = cls(n_qubits=obj["n_qubits"], operations=[], measured=[])
        for o in obj["operations"]:
            if o["op"] == MEASURE:
                model.measured = list(o["qubits"])
            else:
                model.operations.append(
                    Operation(o["op"], tuple(o["qubits"]),
                              float(o["theta"]) if "theta" in o else None))
        return model

    # --- checks ---------------------------------------------------------
    def problems(self) -> list[str]:
        """Problems found locally; an empty list means the structure is valid."""
        found = []
        try:
            validate_circuit(self.to_json_obj())
        except QosError as exc:
            found.append(f"{exc.code}: {exc.message}")
        if self.n_qubits > BACKEND_MAX_QUBITS:
            found.append(
                f"BACKEND_LIMIT: この実行環境で実行できるのは {BACKEND_MAX_QUBITS} 量子ビットまでです")
        return found

    # --- diagram --------------------------------------------------------
    def layout(self) -> list[list[int]]:
        """Pack operations into columns; each column lists operation indices.

        An operation goes into the first column after the last one that uses
        any qubit in its span (a CX blocks every wire between its qubits, so
        the drawn vertical line never crosses another gate).
        """
        last_col = [-1] * self.n_qubits
        columns: list[list[int]] = []
        for i, o in enumerate(self.operations):
            lo, hi = min(o.qubits), max(o.qubits)
            col = max(last_col[lo:hi + 1]) + 1
            if col == len(columns):
                columns.append([])
            columns[col].append(i)
            for q in range(lo, hi + 1):
                last_col[q] = col
        return columns


SAMPLES = {
    "ベル状態（2 量子ビット）": CircuitModel(
        2, [Operation("h", (0,)), Operation("cx", (0, 1))], [0, 1]),
    "GHZ 状態（3 量子ビット）": CircuitModel(
        3, [Operation("h", (0,)), Operation("cx", (0, 1)), Operation("cx", (1, 2))], [0, 1, 2]),
    "重ね合わせ（1 量子ビット）": CircuitModel(1, [Operation("h", (0,))], [0]),
    "回転ゲート RX(π/2)": CircuitModel(1, [Operation("rx", (0,), math.pi / 2)], [0]),
}


def sample(name: str) -> CircuitModel:
    s = SAMPLES[name]
    return CircuitModel(s.n_qubits, list(s.operations), list(s.measured))
