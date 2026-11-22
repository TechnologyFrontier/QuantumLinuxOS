"""Local state-vector backend ``local_statevector`` (QOS-FD OS-04/05).

* complex128 amplitudes, initial state |0…0⟩,
* qubit 0 is the least-significant bit of the basis index,
* the unmeasured qubits are marginalised and ``shots`` samples are drawn
  with replacement from the resulting distribution,
* bitstring character *i* is the value of ``measured_qubits[i]``.

The sampler is NumPy's PCG64 seeded with the 64-bit seed.  Counts are
reproducible for the same OS image, service build and environment; no
cross-environment guarantee is made (QOS-FD 2.1).
"""

from __future__ import annotations

import math

import numpy as np

from .circuit import Circuit

BACKEND_ID = "local_statevector"

_INV_SQRT2 = 1.0 / math.sqrt(2.0)
_H = np.array([[_INV_SQRT2, _INV_SQRT2], [_INV_SQRT2, -_INV_SQRT2]], dtype=np.complex128)
_X = np.array([[0, 1], [1, 0]], dtype=np.complex128)
_Z = np.array([[1, 0], [0, -1]], dtype=np.complex128)


def _rx(theta: float) -> np.ndarray:
    c, s = math.cos(theta / 2.0), math.sin(theta / 2.0)
    return np.array([[c, -1j * s], [-1j * s, c]], dtype=np.complex128)


def _rz(theta: float) -> np.ndarray:
    return np.array(
        [[np.exp(-0.5j * theta), 0], [0, np.exp(0.5j * theta)]], dtype=np.complex128
    )


def _axis(n: int, q: int) -> int:
    # state.reshape((2,)*n) in C order: axis 0 is the most significant bit.
    return n - 1 - q


def _apply_1q(state: np.ndarray, n: int, q: int, m: np.ndarray) -> np.ndarray:
    ax = _axis(n, q)
    state = np.tensordot(m, state, axes=([1], [ax]))  # new axis 0
    return np.moveaxis(state, 0, ax)


def _apply_cx(state: np.ndarray, n: int, control: int, target: int) -> np.ndarray:
    ca, ta = _axis(n, control), _axis(n, target)
    idx: list = [slice(None)] * n
    idx[ca] = 1
    sub = state[tuple(idx)]  # view with control=1; target axis shifts if after control
    t = ta if ta < ca else ta - 1
    state[tuple(idx)] = np.flip(sub, axis=t).copy()
    return state


def statevector(circuit: Circuit) -> np.ndarray:
    n = circuit.n_qubits
    state = np.zeros((2,) * n, dtype=np.complex128)
    state[(0,) * n] = 1.0
    for g in circuit.gates:
        if g.name == "h":
            state = _apply_1q(state, n, g.qubits[0], _H)
        elif g.name == "x":
            state = _apply_1q(state, n, g.qubits[0], _X)
        elif g.name == "z":
            state = _apply_1q(state, n, g.qubits[0], _Z)
        elif g.name == "rx":
            state = _apply_1q(state, n, g.qubits[0], _rx(g.theta))
        elif g.name == "rz":
            state = _apply_1q(state, n, g.qubits[0], _rz(g.theta))
        elif g.name == "cx":
            state = _apply_cx(state, n, g.qubits[0], g.qubits[1])
        else:  # pragma: no cover - validated earlier
            raise ValueError(f"unsupported gate {g.name}")
    return state


def marginal_probabilities(circuit: Circuit, state: np.ndarray) -> np.ndarray:
    """Probabilities indexed so that measured_qubits[0] is the MSB."""
    n = circuit.n_qubits
    probs = np.abs(state) ** 2
    keep = [_axis(n, q) for q in circuit.measured_qubits]
    drop = tuple(ax for ax in range(n) if ax not in keep)
    if drop:
        probs = probs.sum(axis=drop)
    # remaining axes are in ascending original-axis order; reorder to `keep`
    remaining = sorted(keep)
    probs = np.transpose(probs, [remaining.index(ax) for ax in keep])
    flat = probs.reshape(-1)
    total = flat.sum()
    if not np.isfinite(total) or total <= 0:
        raise ArithmeticError("invalid probability distribution")
    return flat / total


def run(circuit: Circuit, shots: int, seed: int) -> list[list]:
    """Return ``[[bitstring, count], ...]`` sorted lexicographically."""
    state = statevector(circuit)
    probs = marginal_probabilities(circuit, state)
    rng = np.random.Generator(np.random.PCG64(seed))
    counts = rng.multinomial(shots, probs)
    k = len(circuit.measured_qubits)
    nz = np.flatnonzero(counts)  # ascending index == lexicographic for fixed width
    return [[format(int(i), f"0{k}b"), int(counts[i])] for i in nz]
