"""Quantum circuit studio logic (QOS-DT 3.1, T-DT-02/03)."""

import math
import os
import stat
import sys
import textwrap
import time

import pytest

from quantumos.desktop import circuit_model as cm
from quantumos.desktop import qosctl_client as qc

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


# --- circuit model ---------------------------------------------------------

def test_bell_sample_round_trips_and_is_valid():
    m = cm.sample("ベル状態（2 量子ビット）")
    assert m.problems() == []
    text = m.to_json_text()
    again = cm.CircuitModel.from_json_text(text)
    assert again.to_json_obj() == m.to_json_obj()
    assert again.to_json_obj()["operations"][-1] == {"op": "measure", "qubits": [0, 1]}


def test_samples_are_independent_copies():
    a = cm.sample("ベル状態（2 量子ビット）")
    a.add("x", (0,))
    assert len(cm.sample("ベル状態（2 量子ビット）").operations) == 2


def test_problems_use_service_rules():
    m = cm.CircuitModel(2, [], [])
    assert any("measure needs at least one qubit" in p for p in m.problems())
    m = cm.CircuitModel(2, [cm.Operation("cx", (1, 1))], [0])
    assert any("duplicate qubit" in p for p in m.problems())


def test_backend_limit_is_reported():
    m = cm.CircuitModel(21, [cm.Operation("h", (0,))], [0])
    assert any(p.startswith("BACKEND_LIMIT") for p in m.problems())


def test_set_qubits_drops_out_of_range_and_extends_full_measure():
    m = cm.sample("ベル状態（2 量子ビット）")
    m.set_qubits(3)
    assert m.measured == [0, 1, 2]
    m.add("x", (2,))
    m.set_qubits(1)
    assert [o.op for o in m.operations] == ["h"]
    assert m.measured == [0]
    with pytest.raises(ValueError):
        m.set_qubits(0)


def test_move_and_remove():
    m = cm.CircuitModel(2, [], [0, 1])
    m.add("h", (0,))
    m.add("x", (1,))
    assert m.move(1, -1) == 0
    assert [o.op for o in m.operations] == ["x", "h"]
    assert m.move(0, -1) == 0  # no-op at the edge
    m.remove(0)
    assert [o.op for o in m.operations] == ["h"]


def test_rotation_json_and_theta_parsing():
    m = cm.CircuitModel(1, [], [0])
    m.add("rx", (0,), cm.parse_theta("pi/2"))
    assert m.to_json_obj()["operations"][0] == {"op": "rx", "qubits": [0], "theta": math.pi / 2}
    assert cm.parse_theta("-π/4") == pytest.approx(-math.pi / 4)
    assert cm.parse_theta("0.5pi") == pytest.approx(math.pi / 2)
    assert cm.parse_theta("2*pi") == pytest.approx(2 * math.pi)
    assert cm.parse_theta("1.25") == 1.25
    for bad in ("", "pi/pi", "abc", "inf"):
        with pytest.raises(ValueError):
            cm.parse_theta(bad)
    assert cm.format_theta(math.pi / 2) == "π/2"


def test_from_json_rejects_invalid_files():
    with pytest.raises(ValueError, match="SCHEMA_UNSUPPORTED"):
        cm.CircuitModel.from_json_text('{"schema_version":2,"n_qubits":1,"operations":[]}')
    with pytest.raises(ValueError, match="JSON"):
        cm.CircuitModel.from_json_text('{"schema_version":1,')
    with pytest.raises(ValueError):
        cm.CircuitModel.from_json_text(b"\xff\xfe")


def test_examples_load(tmp_path):
    here = os.path.join(os.path.dirname(__file__), "..", "examples")
    for name in os.listdir(here):
        with open(os.path.join(here, name), "rb") as fh:
            assert cm.CircuitModel.from_json_text(fh.read()).problems() == []


def test_layout_packs_parallel_gates_and_blocks_cx_span():
    m = cm.CircuitModel(3, [], [0, 1, 2])
    m.add("h", (0,))       # col 0
    m.add("h", (2,))       # col 0 (parallel)
    m.add("cx", (0, 2))    # col 1, spans q1 too
    m.add("x", (1,))       # col 2 (q1 blocked by the cx line)
    assert m.layout() == [[0, 1], [2], [3]]


def test_every_editable_gate_is_a_service_gate():
    from quantumos.circuit import ALL_OPS, MEASURE
    assert set(cm.EDITABLE_GATES) | {MEASURE} == ALL_OPS


# --- qosctl contract -------------------------------------------------------

RESULT = b'{"counts":[["00",505],["11",519]],"shots":1024,"seed":"14045576854515700417","measured_qubits":[0,1],"backend_id":"statevector-v1","service_version":"0.1.0"}\n'


def test_parse_run_result():
    r = qc.parse_run_result(RESULT)
    assert r.seed == "14045576854515700417"
    assert r.rows() == [("00", 505, 505 / 1024), ("11", 519, 519 / 1024)]
    assert r.raw_line == RESULT.decode().strip()


@pytest.mark.parametrize("bad", [
    b"",
    RESULT + RESULT,
    b'{"counts":[["00",5]],"shots":6,"seed":"1","measured_qubits":[0,1]}',
    b'{"counts":[["0",6]],"shots":6,"seed":"1","measured_qubits":[0,1]}',
    b'{"counts":[["00",6]],"shots":6,"seed":1,"measured_qubits":[0,1]}',
    b'{"counts":[["0a",6]],"shots":6,"seed":"1","measured_qubits":[0,1]}',
])
def test_parse_run_result_rejects_inconsistent_output(bad):
    with pytest.raises((qc.ResponseError, ValueError)):
        qc.parse_run_result(bad)


def test_failure_parsing_and_guidance():
    f = qc.parse_failure(b'{"code":"BUSY","message":"busy"}\n', 3)
    assert (f.code, f.exit_status, f.category) == ("BUSY", 3, "利用できない")
    assert "自動では再試行しません" in f.guidance
    f = qc.parse_failure(b"Traceback...\nboom\n", 4)
    assert f.code == "TRANSPORT_ERROR"


def test_run_argv_passes_arguments_individually():
    argv = qc.run_argv("/usr/bin/qosctl", "/home/a/量子 回路/x.json", 10, "0")
    assert argv == ["/usr/bin/qosctl", "run", "/home/a/量子 回路/x.json", "--shots", "10", "--seed", "0"]
    assert qc.run_argv("q", "f", None, None) == ["q", "run", "f"]


def _fake_qosctl(tmp_path, body):
    p = tmp_path / "fake-qosctl"
    p.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


def test_unexpected_exit_status_is_a_failure(tmp_path):
    client = qc.QosctlClient(_fake_qosctl(tmp_path, "import sys; sys.exit(1)\n"))
    f = client.status()
    assert isinstance(f, qc.Failure) and f.code == "TRANSPORT_ERROR"


def test_missing_qosctl(tmp_path):
    f = qc.QosctlClient(str(tmp_path / "nope")).status()
    assert isinstance(f, qc.Failure) and f.code == "SERVICE_UNAVAILABLE"


def test_stop_kills_the_child(tmp_path):
    client = qc.QosctlClient(_fake_qosctl(tmp_path, "import time; time.sleep(60)\n"))
    inv = client.start_run("x.json", None, None)
    t0 = time.monotonic()
    inv.stop()
    status, out, err = inv.wait(timeout=10)
    assert inv.stopped and status != 0 and time.monotonic() - t0 < 10


# --- against the real CLI and service ---------------------------------------

@pytest.fixture
def real_client(service, tmp_path):
    wrapper = tmp_path / "qosctl"
    wrapper.write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {os.path.abspath(SRC)!r})\n"
        "from quantumos.cli import main\nsys.exit(main())\n")
    wrapper.chmod(0o755)
    env = dict(os.environ, QOS_SOCKET=service.path)
    return qc.QosctlClient(str(wrapper), env=env)


def test_real_status_validate_and_run(real_client, tmp_path):
    assert real_client.status() == "ready"
    path = tmp_path / "量子 回路.json"
    path.write_text(cm.sample("ベル状態（2 量子ビット）").to_json_text(), encoding="utf-8")
    assert real_client.validate(str(path)) is True
    inv = real_client.start_run(str(path), 1024, "7")
    r = real_client.interpret(inv, *inv.wait(), "run")
    assert isinstance(r, qc.RunResult)
    assert {k for k, _ in r.counts} <= {"00", "11"} and r.shots == 1024 and r.seed == "7"
    inv2 = real_client.start_run(str(path), 1024, "7")
    assert real_client.interpret(inv2, *inv2.wait(), "run").counts == r.counts


def test_real_errors(real_client, tmp_path):
    big = cm.CircuitModel(21, [cm.Operation("h", (0,))], [0])
    path = tmp_path / "big.json"
    path.write_text(big.to_json_text())
    inv = real_client.start_run(str(path), None, None)
    f = real_client.interpret(inv, *inv.wait(), "run")
    assert isinstance(f, qc.Failure) and (f.code, f.exit_status) == ("BACKEND_LIMIT", 2)
    inv = real_client.start_run(str(path), None, "01")
    f = real_client.interpret(inv, *inv.wait(), "run")
    assert f.code == "RUN_OPTION_INVALID"
