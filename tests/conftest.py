import os
import socket
import threading
import time

import pytest

from quantumos import protocol
from quantumos.service import QuantumService, ServiceConfig


class RunningService:
    def __init__(self, svc: QuantumService, thread: threading.Thread, path: str):
        self.svc = svc
        self.thread = thread
        self.path = path

    def request(self, body: bytes, timeout: float = 30.0) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(self.path)
            protocol.write_frame(s, body)
            return protocol.parse_response(protocol.read_frame(s, timeout))


def _start(tmp_path, **overrides) -> RunningService:
    path = str(tmp_path / "quantum.sock")
    cfg = ServiceConfig(
        socket_path=path,
        allowed_group=None,
        allowed_uids=[os.getuid()],
        socket_group=None,
        read_timeout=2.0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    svc = QuantumService(cfg)
    t = threading.Thread(target=svc.serve_forever, daemon=True)
    t.start()
    for _ in range(100):
        if os.path.exists(path):
            break
        time.sleep(0.02)
    return RunningService(svc, t, path)


@pytest.fixture
def service_factory(tmp_path):
    started = []

    def factory(**overrides):
        rs = _start(tmp_path, **overrides)
        started.append(rs)
        return rs

    yield factory
    for rs in started:
        rs.svc.stop()
        rs.thread.join(timeout=5)


@pytest.fixture
def service(service_factory):
    return service_factory()
