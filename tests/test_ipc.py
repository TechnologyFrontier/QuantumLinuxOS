"""IPC protocol and service behaviour (QOS-IF 3〜5章, T-OS-02〜05)."""

import json
import socket
import struct
import threading
import time

import pytest

from quantumos import errors, protocol
from quantumos.limits import MIB

X1 = {"schema_version": 1, "n_qubits": 2,
      "operations": [{"op": "x", "qubits": [1]}, {"op": "measure", "qubits": [1, 0]}]}


def req(**kw):
    return json.dumps({"protocol_version": 1, **kw}).encode()


def err_code(resp):
    assert resp["ok"] is False
    return resp["error"]["code"]


def test_status_ready(service):
    assert service.request(req(op="status")) == {
        "protocol_version": 1, "ok": True, "result": {"state": "ready"}}


def test_validate(service):
    assert service.request(req(op="validate", circuit=X1))["result"] == {"valid": True}


def test_run_example_from_spec(service):
    body = (b'{"protocol_version":1,"op":"run","circuit":{"schema_version":1,"n_qubits":2,'
            b'"operations":[{"op":"x","qubits":[1]},{"op":"measure","qubits":[1,0]}]},'
            b'"shots":8,"seed":"0"}')
    assert service.request(body)["result"] == {
        "counts": [["10", 8]], "shots": 8, "seed": "0", "measured_qubits": [1, 0],
        "backend_id": "local_statevector", "service_version": "0.1.0"}


def test_default_shots_and_generated_seed(service):
    r = service.request(req(op="run", circuit=X1))["result"]
    assert r["shots"] == 1024 and r["counts"] == [["10", 1024]]
    assert isinstance(r["seed"], str) and r["seed"].isdigit()


@pytest.mark.parametrize("seed", ["0", "9007199254740993", "18446744073709551615"])
def test_seed_round_trip(service, seed):  # T-OS-04
    bell = {"schema_version": 1, "n_qubits": 2, "operations": [
        {"op": "h", "qubits": [0]}, {"op": "cx", "qubits": [0, 1]},
        {"op": "measure", "qubits": [0, 1]}]}
    a = service.request(req(op="run", circuit=bell, shots=500, seed=seed))["result"]
    b = service.request(req(op="run", circuit=bell, shots=500, seed=seed))["result"]
    assert a["seed"] == seed and a["counts"] == b["counts"]


@pytest.mark.parametrize("body,code", [
    (b"not json", errors.PROTOCOL_INVALID),
    (b"\xff\xfe", errors.PROTOCOL_INVALID),
    (b"[]", errors.PROTOCOL_INVALID),
    (req(op="status")[:-1] + b',"op":"status"}', errors.PROTOCOL_INVALID),  # duplicate key
    (b'{"protocol_version":1,"op":"status","x":NaN}', errors.PROTOCOL_INVALID),
    (b'{"protocol_version":1,"op":"status","x":1e999}', errors.PROTOCOL_INVALID),
    (req(op="status", extra=1), errors.PROTOCOL_INVALID),
    (req(op="status", circuit=X1), errors.PROTOCOL_INVALID),
    (req(op="validate", circuit=X1, shots=1), errors.PROTOCOL_INVALID),
    (req(op="run"), errors.PROTOCOL_INVALID),
    (req(op="explode"), errors.PROTOCOL_INVALID),
    (b'{"op":"status"}', errors.PROTOCOL_INVALID),
    (b'{"protocol_version":"1","op":"status"}', errors.PROTOCOL_INVALID),
    (b'{"protocol_version":1.0,"op":"status"}', errors.PROTOCOL_INVALID),
    (b'{"protocol_version":true,"op":"status"}', errors.PROTOCOL_INVALID),
    (b'{"protocol_version":2,"op":"status"}', errors.PROTOCOL_UNSUPPORTED),
    (req(op="run", circuit={**X1, "schema_version": 2}), errors.SCHEMA_UNSUPPORTED),
    (req(op="run", circuit={**X1, "operations": X1["operations"][:1]}), errors.CIRCUIT_INVALID),
    (req(op="run", circuit=X1, shots=0), errors.RUN_OPTION_INVALID),
    (req(op="run", circuit=X1, shots=100_001), errors.RUN_OPTION_INVALID),
    (req(op="run", circuit=X1, shots=8.0), errors.RUN_OPTION_INVALID),
    (req(op="run", circuit=X1, seed=0), errors.RUN_OPTION_INVALID),
    (req(op="run", circuit=X1, seed=None), errors.RUN_OPTION_INVALID),
    (req(op="run", circuit=X1, seed="18446744073709551616"), errors.RUN_OPTION_INVALID),
])
def test_request_errors(service, body, code):
    assert err_code(service.request(body)) == code


def test_check_order_circuit_before_options(service):
    bad = {**X1, "n_qubits": 0}
    assert err_code(service.request(req(op="run", circuit=bad, shots=0))) == errors.CIRCUIT_INVALID
    big = {"schema_version": 1, "n_qubits": 21, "operations": [{"op": "measure", "qubits": [0]}]}
    assert err_code(service.request(req(op="run", circuit=big, shots=0))) == errors.RUN_OPTION_INVALID
    assert err_code(service.request(req(op="run", circuit=big))) == errors.BACKEND_LIMIT
    # validate does not apply the backend limit
    assert service.request(req(op="validate", circuit=big))["ok"] is True


def test_circuit_4mib_limit_measured_on_request_bytes(service):
    head = b'{"protocol_version":1,"op":"validate","circuit":'
    circuit = json.dumps(X1).encode()
    pad = 4 * MIB - len(circuit)
    at_limit = circuit[:-1] + b" " * pad + b"}"
    assert len(at_limit) == 4 * MIB
    assert service.request(head + at_limit + b"}")["ok"] is True
    over = circuit[:-1] + b" " * (pad + 1) + b"}"
    assert err_code(service.request(head + over + b"}")) == errors.CIRCUIT_INVALID


def _raw(path, data, read=True, timeout=5):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(data)
        if not read:
            return None
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk


def _frames(data):
    out = []
    while data:
        (n,) = struct.unpack(">I", data[:4])
        out.append(json.loads(data[4:4 + n]))
        data = data[4 + n:]
    return out


def test_only_first_frame_is_processed(service):
    two = protocol.encode_frame(req(op="status")) + protocol.encode_frame(req(op="status"))
    assert len(_frames(_raw(service.path, two))) == 1


@pytest.mark.parametrize("data", [
    struct.pack(">I", 0),
    struct.pack(">I", 8 * MIB + 1),
    struct.pack(">I", 100) + b"{}",  # truncated, then peer half-waits
    b"\x00\x00",
])
def test_bad_frames_close_without_response(service, data):
    assert _raw(service.path, data) == b""


def test_8mib_frame_limit(service):
    body = req(op="status")
    ok = body[:-1] + b" " * (8 * MIB - len(body)) + b"}"
    assert len(ok) == 8 * MIB
    assert _frames(_raw(service.path, protocol.encode_frame(ok)))[0]["ok"] is True


def test_access_denied(service_factory):  # T-OS-02
    svc = service_factory(allowed_uids=[], allowed_group=None)
    assert err_code(svc.request(req(op="status"))) == errors.ACCESS_DENIED


def test_body_cannot_claim_identity(service_factory):
    svc = service_factory(allowed_uids=[], allowed_group=None)
    body = b'{"protocol_version":1,"op":"status","uid":0}'
    assert err_code(svc.request(body)) == errors.ACCESS_DENIED


def _slow_circuit():
    # 20 qubits x many gates: long enough to overlap with other requests
    ops = [{"op": "h", "qubits": [q % 20]} for q in range(9_999)]
    ops.append({"op": "measure", "qubits": [0]})
    return {"schema_version": 1, "n_qubits": 20, "operations": ops}


def test_busy_and_timeout_then_recovery(service_factory):  # T-OS-05
    svc = service_factory(execution_timeout=1.0)
    result = {}

    def long_run():
        result["r"] = svc.request(req(op="run", circuit=_slow_circuit(), shots=1))

    t = threading.Thread(target=long_run)
    t.start()
    for _ in range(100):
        if svc.svc.slot.locked():
            break
        time.sleep(0.01)
    assert svc.request(req(op="status"))["result"] == {"state": "busy"}
    assert err_code(svc.request(req(op="run", circuit=X1, shots=1))) == errors.BUSY
    t.join(10)
    assert err_code(result["r"]) == errors.TIMEOUT
    assert "result" not in result["r"]
    assert svc.request(req(op="status"))["result"] == {"state": "ready"}
    assert svc.request(req(op="run", circuit=X1, shots=1))["ok"] is True


def test_disconnect_releases_slot(service_factory):
    svc = service_factory(execution_timeout=60.0)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(svc.path)
    protocol.write_frame(s, req(op="run", circuit=_slow_circuit(), shots=1))
    for _ in range(100):
        if svc.svc.slot.locked():
            break
        time.sleep(0.01)
    assert svc.svc.slot.locked()
    s.close()
    for _ in range(300):
        if not svc.svc.slot.locked():
            break
        time.sleep(0.01)
    assert not svc.svc.slot.locked()
    assert svc.request(req(op="run", circuit=X1, shots=1))["ok"] is True


def test_socket_permissions(service_factory, tmp_path):
    import os, stat
    svc = service_factory()
    assert stat.S_IMODE(os.stat(svc.path).st_mode) == 0o660
