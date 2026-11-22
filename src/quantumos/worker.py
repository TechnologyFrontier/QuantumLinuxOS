"""Compute worker process.

The service runs every ``run`` in a fresh child process so that a timeout,
client disconnect or memory exhaustion can be handled by terminating the
child and confirming its exit before the compute slot is released
(QOS-FD 2.2).

Input on stdin (one JSON document)::

    {"n_qubits": N, "gates": [[name, [q...], theta|null], ...],
     "measured_qubits": [...], "shots": S, "seed": "decimal"}

Output on stdout: ``{"counts": [[bits, n], ...]}``.  Any failure → exit ≠ 0.
"""

from __future__ import annotations

import json
import os
import sys

from .circuit import Circuit, Gate
from . import simulator


def encode_job(circuit: Circuit, shots: int, seed: int) -> bytes:
    return json.dumps(
        {
            "n_qubits": circuit.n_qubits,
            "gates": [[g.name, list(g.qubits), g.theta] for g in circuit.gates],
            "measured_qubits": list(circuit.measured_qubits),
            "shots": shots,
            "seed": str(seed),
        },
        separators=(",", ":"),
    ).encode("utf-8")


def decode_job(data: bytes) -> tuple[Circuit, int, int]:
    job = json.loads(data)
    gates = tuple(Gate(name, tuple(qs), theta) for name, qs, theta in job["gates"])
    circuit = Circuit(int(job["n_qubits"]), gates, tuple(job["measured_qubits"]))
    return circuit, int(job["shots"]), int(job["seed"])


def _apply_memory_limit() -> None:
    limit = os.environ.get("QOS_WORKER_MEMORY_LIMIT")
    if limit:
        import resource

        n = int(limit)
        resource.setrlimit(resource.RLIMIT_AS, (n, n))


def main() -> int:
    try:
        _apply_memory_limit()
        circuit, shots, seed = decode_job(sys.stdin.buffer.read())
        counts = simulator.run(circuit, shots, seed)
    except MemoryError:
        return 3
    except Exception:  # noqa: BLE001 - reported to the service as a failure
        return 2
    sys.stdout.write(json.dumps({"counts": counts}, separators=(",", ":")))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
