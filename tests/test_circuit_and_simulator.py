"""OS-03 validation and OS-04/05 computation."""

import math

import numpy as np
import pytest

from quantumos import errors, simulator
from quantumos.circuit import validate_circuit
from quantumos.errors import QosError
from quantumos.seed import parse_seed


def circ(n, *ops, sv=1):
    return {"schema_version": sv, "n_qubits": n, "operations": list(ops)}


def op(name, *qubits, **kw):
    return {"op": name, "qubits": list(qubits), **kw}


def code_of(obj):
    with pytest.raises(QosError) as ei:
        validate_circuit(obj)
    return ei.value.code


# --- validation -----------------------------------------------------------
def test_valid_minimal():
    c = validate_circuit(circ(2, op("x", 1), op("measure", 1, 0)))
    assert c.measured_qubits == (1, 0)
    assert len(c.gates) == 1


@pytest.mark.parametrize(
    "obj",
    [
        [],
        circ(0, op("measure", 0)),
        circ(65, op("measure", 0)),
        circ(True, op("measure", 0)),
        circ(1),  # no operations
        circ(1, op("x", 0)),  # no terminal measurement
        circ(2, op("measure", 0), op("x", 1)),  # measure not last
        circ(2, op("measure", 0), op("measure", 1)),
        circ(1, op("measure")),
        circ(2, op("measure", 0, 0)),
        circ(1, op("x", 1), op("measure", 0)),  # out of range
        circ(1, op("x", -1), op("measure", 0)),
        circ(1, op("x", True), op("measure", 0)),
        circ(2, op("x", 0, 1), op("measure", 0)),
        circ(2, op("cx", 0), op("measure", 0)),
        circ(2, op("cx", 1, 1), op("measure", 0)),
        circ(1, op("rx", 0), op("measure", 0)),  # theta missing
        circ(1, op("rx", 0, theta="1"), op("measure", 0)),
        circ(1, op("rx", 0, theta=True), op("measure", 0)),
        circ(1, op("h", 0, theta=0.1), op("measure", 0)),  # theta on non-rotation
        circ(1, op("y", 0), op("measure", 0)),
        circ(1, op("x", 0, label="a"), op("measure", 0)),
        {**circ(1, op("measure", 0)), "seed": "0"},  # no seed inside circuits
        {"n_qubits": 1, "operations": [op("measure", 0)]},
        circ(1, op("measure", 0), sv="1"),
        circ(1, op("measure", 0), sv=1.0),
    ],
)
def test_invalid_circuits(obj):
    assert code_of(obj) == errors.CIRCUIT_INVALID


@pytest.mark.parametrize("sv", [0, 2, 99])
def test_unsupported_schema(sv):
    assert code_of(circ(1, op("measure", 0), sv=sv)) == errors.SCHEMA_UNSUPPORTED


def test_operation_count_limit():
    ops = [op("x", 0)] * 9_999 + [op("measure", 0)]
    validate_circuit(circ(1, *ops))
    assert code_of(circ(1, *([op("x", 0)] * 10_000 + [op("measure", 0)]))) == errors.CIRCUIT_INVALID


def test_64_qubits_structurally_valid():
    validate_circuit(circ(64, op("x", 63), op("measure", 63)))


# --- seeds ----------------------------------------------------------------
@pytest.mark.parametrize("s,n", [("0", 0), ("9007199254740993", 2**53 + 1),
                                 ("18446744073709551615", 2**64 - 1)])
def test_seed_ok(s, n):
    assert parse_seed(s) == n


@pytest.mark.parametrize("s", ["", "00", "01", "-1", "+1", " 1", "1 ", "1e3", "1.0",
                               "18446744073709551616", "１", 0, None, 1.5])
def test_seed_rejected(s):
    with pytest.raises(ValueError):
        parse_seed(s)


# --- simulation -----------------------------------------------------------
def run(obj, shots=100, seed=0):
    return simulator.run(validate_circuit(obj), shots, seed)


def test_x1_measure_10():  # T-OS-03
    assert run(circ(2, op("x", 1), op("measure", 1, 0)), shots=8) == [["10", 8]]


def test_measure_order_and_lsb_convention():
    c = validate_circuit(circ(3, op("x", 0), op("measure", 0, 1, 2)))
    assert simulator.run(c, 5, 1) == [["100", 5]]
    sv = simulator.statevector(c).reshape(-1)
    assert abs(sv[1]) == pytest.approx(1.0)  # qubit 0 is the LSB of the basis index


def test_bell_state_correlated_and_sorted():
    counts = run(circ(2, op("h", 0), op("cx", 0, 1), op("measure", 0, 1)), shots=2000, seed=7)
    assert [b for b, _ in counts] == ["00", "11"]
    assert sum(n for _, n in counts) == 2000
    assert all(700 < n < 1300 for _, n in counts)


def test_cx_control_target_direction():
    assert run(circ(2, op("x", 1), op("cx", 1, 0), op("measure", 0, 1))) == [["11", 100]]
    assert run(circ(2, op("x", 0), op("cx", 1, 0), op("measure", 0, 1))) == [["10", 100]]


def test_marginalisation():
    counts = run(circ(3, op("x", 2), op("h", 0), op("measure", 2)), shots=50)
    assert counts == [["1", 50]]


def test_rotations():
    sv = simulator.statevector(validate_circuit(circ(1, op("rx", 0, theta=math.pi), op("measure", 0))))
    assert np.allclose(np.abs(sv.reshape(-1)), [0, 1])
    sv = simulator.statevector(
        validate_circuit(circ(1, op("h", 0), op("rz", 0, theta=math.pi), op("h", 0), op("measure", 0)))
    )
    assert np.allclose(np.abs(sv.reshape(-1)), [0, 1])
    assert run(circ(1, op("h", 0), op("z", 0), op("h", 0), op("measure", 0))) == [["1", 100]]


def test_reproducible_with_same_seed():
    c = circ(3, op("h", 0), op("h", 1), op("rx", 2, theta=0.3), op("measure", 0, 1, 2))
    assert run(c, 1000, 2**64 - 1) == run(c, 1000, 2**64 - 1)
    assert run(c, 1000, 1) != run(c, 1000, 2)


def test_20_qubits_runs():
    ops = [op("h", q) for q in range(20)] + [op("measure", *range(20))]
    counts = run(circ(20, *ops), shots=10, seed=3)
    assert sum(n for _, n in counts) == 10
