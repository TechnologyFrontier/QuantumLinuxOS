"""qosctl output and exit-status contract (QOS-IF 2章)."""

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
X1 = '{"schema_version":1,"n_qubits":2,"operations":[{"op":"x","qubits":[1]},{"op":"measure","qubits":[1,0]}]}'


def qosctl(sock, *args):
    env = {**os.environ, "PYTHONPATH": os.path.join(ROOT, "src"), "QOS_SOCKET": sock}
    p = subprocess.run([sys.executable, "-m", "quantumos", *args],
                       capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, p.stdout, p.stderr


@pytest.fixture
def x1(tmp_path):
    p = tmp_path / "x1.json"
    p.write_text(X1)
    return str(p)


def one_json_line(text):
    lines = text.splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_run_success(service, x1):
    rc, out, err = qosctl(service.path, "run", x1, "--shots", "8", "--seed", "0")
    assert rc == 0 and err == ""
    r = one_json_line(out)
    assert r["counts"] == [["10", 8]] and r["seed"] == "0"


def test_validate_and_status(service, x1):
    assert one_json_line(qosctl(service.path, "validate", x1)[1]) == {"valid": True}
    assert one_json_line(qosctl(service.path, "status")[1]) == {"state": "ready"}


@pytest.mark.parametrize("args,code", [
    (["run", "{f}", "--shots", "0"], "RUN_OPTION_INVALID"),
    (["run", "{f}", "--shots", "100001"], "RUN_OPTION_INVALID"),
    (["run", "{f}", "--shots", "1.5"], "RUN_OPTION_INVALID"),
    (["run", "{f}", "--seed", "-1"], "RUN_OPTION_INVALID"),
    (["run", "{f}", "--seed", "18446744073709551616"], "RUN_OPTION_INVALID"),
    (["run", "/nonexistent.json"], "FILE_UNREADABLE"),
    (["frobnicate"], "USAGE_INVALID"),
    (["run"], "USAGE_INVALID"),
])
def test_exit_2_input_errors(service, x1, args, code):
    rc, out, err = qosctl(service.path, *[a.replace("{f}", x1) for a in args])
    assert rc == 2 and out == ""
    assert one_json_line(err)["code"] == code


def test_bad_circuit_file(service, tmp_path):
    p = tmp_path / "dup.json"
    p.write_text('{"schema_version":1,"schema_version":1,"n_qubits":1,"operations":[]}')
    rc, out, err = qosctl(service.path, "validate", str(p))
    assert (rc, out) == (2, "") and one_json_line(err)["code"] == "CIRCUIT_INVALID"
    p.write_bytes(b" " * (4 * 1024 * 1024) + b"{}")
    rc, _, err = qosctl(service.path, "validate", str(p))
    assert rc == 2 and one_json_line(err)["code"] == "CIRCUIT_INVALID"


def test_backend_limit_is_exit_2(service, tmp_path):
    p = tmp_path / "big.json"
    p.write_text('{"schema_version":1,"n_qubits":21,"operations":[{"op":"measure","qubits":[0]}]}')
    rc, _, err = qosctl(service.path, "run", str(p))
    assert rc == 2 and one_json_line(err)["code"] == "BACKEND_LIMIT"


def test_exit_3_service_unavailable(tmp_path, x1):
    rc, out, err = qosctl(str(tmp_path / "none.sock"), "status")
    assert (rc, out) == (3, "") and one_json_line(err)["code"] == "SERVICE_UNAVAILABLE"


def test_exit_3_access_denied(service_factory):
    svc = service_factory(allowed_uids=[], allowed_group=None)
    rc, _, err = qosctl(svc.path, "status")
    assert rc == 3 and one_json_line(err)["code"] == "ACCESS_DENIED"


def test_exit_4_transport_error(tmp_path):
    import socket, threading
    path = str(tmp_path / "evil.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def evil():
        c, _ = srv.accept()
        c.recv(1024)
        c.sendall(b"\x00\x00\x00\x10{\"ok\":")  # truncated frame
        c.close()

    threading.Thread(target=evil, daemon=True).start()
    rc, out, err = qosctl(path, "status")
    srv.close()
    assert (rc, out) == (4, "") and one_json_line(err)["code"] == "TRANSPORT_ERROR"
