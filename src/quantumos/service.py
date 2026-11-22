"""``qosd`` – the quantum service (QOS-FD OS-02〜05).

* listens only on a local Unix stream socket (no network listener),
* one frame per connection, then close,
* authorises by the kernel-supplied peer credentials (SO_PEERCRED) against
  the ``quantum`` group – never by anything in the request body,
* one compute slot for the whole service, no queue (BUSY otherwise),
* each run executes in a child process with a 120 s deadline; the child is
  killed and reaped before the slot is released,
* logs only op, error code, duration and sizes – never circuits, results
  or seeds.
"""

from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import pwd
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from . import __version__, errors, protocol, seed as seedmod, simulator, worker
from .circuit import validate_circuit
from .errors import QosError
from .limits import EXECUTION_TIMEOUT_SEC

log = logging.getLogger("qosd")

DEFAULT_CONFIG_PATH = "/etc/quantumos/qosd.conf"
SD_LISTEN_FDS_START = 3


@dataclass
class ServiceConfig:
    socket_path: str = protocol.DEFAULT_SOCKET_PATH
    allowed_group: str | None = "quantum"
    allowed_uids: list[int] = field(default_factory=list)
    execution_timeout: float = EXECUTION_TIMEOUT_SEC
    read_timeout: float = 10.0  # provisional connection read deadline
    max_connections: int = 32
    socket_mode: int = 0o660
    socket_group: str | None = "quantum"
    worker_memory_limit: int | None = None  # bytes (RLIMIT_AS), optional

    @classmethod
    def load(cls, path: str | None) -> "ServiceConfig":
        cfg = cls()
        if path and os.path.exists(path):
            with open(path, "rb") as fh:
                data = json.load(fh)
            for key, value in data.items():
                if not hasattr(cfg, key):
                    raise ValueError(f"unknown config key: {key}")
                setattr(cfg, key, value)
        return cfg


# --------------------------------------------------------------------------
# Authorisation (OS-02)
# --------------------------------------------------------------------------
_UCRED = struct.Struct("3i")


def peer_credentials(conn: socket.socket) -> tuple[int, int, int]:
    data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    return _UCRED.unpack(data)  # pid, uid, gid


class Authorizer:
    def __init__(self, cfg: ServiceConfig):
        self.group = cfg.allowed_group
        self.uids = set(cfg.allowed_uids)

    def is_allowed(self, uid: int, gid: int) -> bool:
        if uid in self.uids:
            return True
        if not self.group:
            return False
        try:
            g = grp.getgrnam(self.group)
        except KeyError:
            return False
        if gid == g.gr_gid:
            return True
        try:
            pw = pwd.getpwuid(uid)
        except KeyError:
            return False
        if pw.pw_gid == g.gr_gid:
            return True
        try:
            return g.gr_gid in os.getgrouplist(pw.pw_name, pw.pw_gid)
        except OSError:
            return pw.pw_name in g.gr_mem


# --------------------------------------------------------------------------
# Compute execution
# --------------------------------------------------------------------------
class ClientGone(Exception):
    pass


def execute_in_worker(
    job: bytes,
    timeout: float,
    conn: socket.socket | None = None,
    memory_limit: int | None = None,
) -> list:
    """Run a job in a child process.  Returns counts or raises ``QosError``.

    ``conn`` is watched for disconnect; on disconnect ``ClientGone`` is raised.
    In every case the child is terminated (if still running) and reaped
    before this function returns, so the caller may release the slot.
    """
    env = dict(os.environ)
    env.pop("LISTEN_FDS", None)
    env.pop("LISTEN_PID", None)
    # make sure the child imports this very package (source tree or install)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (pkg_parent, env.get("PYTHONPATH", "")) if p
    )
    if memory_limit:
        env["QOS_WORKER_MEMORY_LIMIT"] = str(int(memory_limit))
    proc = subprocess.Popen(
        [sys.executable, "-m", "quantumos.worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
        close_fds=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    out = bytearray()
    deadline = time.monotonic() + timeout
    try:
        try:
            proc.stdin.write(job)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        out_fd = proc.stdout.fileno()
        watch = [out_fd] + ([conn.fileno()] if conn is not None else [])
        while True:  # read until the worker closes stdout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QosError(errors.TIMEOUT, "execution deadline exceeded")
            ready, _, _ = select.select(watch, [], [], min(remaining, 0.5))
            if conn is not None and conn.fileno() in ready and _client_closed(conn):
                raise ClientGone()
            if out_fd in ready:
                chunk = os.read(out_fd, 1 << 20)
                if not chunk:
                    break
                out += chunk
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0.01))
        except subprocess.TimeoutExpired as exc:
            raise QosError(errors.TIMEOUT, "execution deadline exceeded") from exc
        if proc.returncode != 0:
            raise QosError(errors.EXECUTION_FAILED, "computation failed")
        try:
            return json.loads(out)["counts"]
        except (ValueError, KeyError, TypeError) as exc:
            raise QosError(errors.EXECUTION_FAILED, "computation failed") from exc
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait()  # confirm the worker has exited before releasing the slot
        proc.stdout.close()


def _finish(conn: socket.socket, limit: int = 2 * 8 * 1024 * 1024) -> None:
    """Half-close and discard anything the client already queued (frames
    after the first are never processed).  Closing an AF_UNIX socket with
    unread data would otherwise reset the peer and could drop the response."""
    conn.shutdown(socket.SHUT_WR)
    drained = 0
    while drained < limit:
        try:
            data = conn.recv(65536, socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            return
        if not data:
            return
        drained += len(data)


def _client_closed(conn: socket.socket) -> bool:
    """True if the peer closed the connection.  Extra bytes are discarded:
    frames after the first are never processed."""
    try:
        data = conn.recv(65536, socket.MSG_DONTWAIT)
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True
    return data == b""


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class QuantumService:
    def __init__(self, cfg: ServiceConfig):
        self.cfg = cfg
        self.auth = Authorizer(cfg)
        self.slot = threading.Lock()
        self.conn_sem = threading.BoundedSemaphore(cfg.max_connections)
        self._stop = threading.Event()
        self.listener: socket.socket | None = None

    # -- request handling -------------------------------------------------
    def handle_body(
        self, body: bytes, authorized: bool, conn=None, info: dict | None = None
    ) -> bytes:
        if not authorized:
            raise QosError(errors.ACCESS_DENIED, "access denied")
        req = protocol.parse_request(body)
        if info is not None:
            info["op"] = req.op
            if req.circuit is not None:
                info["n_ops"] = len(req.circuit.gates) + 1
                info["shots"] = req.shots if req.op == "run" else 0
        if req.op == "status":
            return protocol.ok_response({"state": "busy" if self.slot.locked() else "ready"})
        if req.op == "validate":
            return protocol.ok_response({"valid": True})
        # run
        if not self.slot.acquire(blocking=False):
            raise QosError(errors.BUSY, "compute slot in use")
        try:
            if req.seed is None:
                try:
                    seed = seedmod.generate_seed()
                except Exception as exc:  # noqa: BLE001
                    raise QosError(errors.EXECUTION_FAILED, "random source failure") from exc
            else:
                seed = req.seed
            job = worker.encode_job(req.circuit, req.shots, seed)
            counts = execute_in_worker(
                job, self.cfg.execution_timeout, conn, self.cfg.worker_memory_limit
            )
        finally:
            self.slot.release()
        return protocol.ok_response(
            {
                "counts": counts,
                "shots": req.shots,
                "seed": seedmod.format_seed(seed),
                "measured_qubits": list(req.circuit.measured_qubits),
                "backend_id": simulator.BACKEND_ID,
                "service_version": __version__,
            }
        )

    def handle_connection(self, conn: socket.socket) -> None:
        started = time.monotonic()
        info: dict = {"op": "-", "n_ops": 0, "shots": 0}
        code, uid = "OK", -1
        try:
            try:
                _pid, uid, gid = peer_credentials(conn)
                authorized = self.auth.is_allowed(uid, gid)
            except OSError:
                authorized = False
            try:
                body = protocol.read_frame(conn, timeout=self.cfg.read_timeout)
            except protocol.FrameError as exc:
                code = "FRAME_REJECTED"
                log.info("connection closed: %s", exc)
                return
            conn.settimeout(None)
            try:
                response = self.handle_body(body, authorized, conn, info)
            except ClientGone:
                code = "CLIENT_DISCONNECTED"
                return
            except QosError as exc:
                code = exc.code
                response = protocol.error_response(exc.code, exc.message)
            except Exception:  # noqa: BLE001
                log.exception("internal error")
                code = errors.EXECUTION_FAILED
                response = protocol.error_response(errors.EXECUTION_FAILED, "internal error")
            if len(response) > protocol.FRAME_MAX_BYTES:
                code = errors.EXECUTION_FAILED
                response = protocol.error_response(
                    errors.EXECUTION_FAILED, "result exceeds the response size limit"
                )
            try:
                protocol.write_frame(conn, response)
                _finish(conn)
            except OSError:
                code = "CLIENT_DISCONNECTED"
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            log.info(
                "op=%s uid=%d result=%s duration_ms=%d n_ops=%d shots=%d",
                info["op"], uid, code, elapsed_ms, info["n_ops"], info["shots"],
            )
            try:
                conn.close()
            finally:
                self.conn_sem.release()

    # -- socket lifecycle ---------------------------------------------------
    def open_listener(self) -> socket.socket:
        if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get(
            "LISTEN_FDS"
        ) == "1":
            sock = socket.socket(fileno=SD_LISTEN_FDS_START)
            log.info("using socket-activated listener")
            return sock
        path = self.cfg.socket_path
        try:
            st = os.lstat(path)
            if stat.S_ISSOCK(st.st_mode):
                os.unlink(path)
            else:
                raise RuntimeError(f"{path} exists and is not a socket")
        except FileNotFoundError:
            pass
        # Bind under a temporary name, fix ownership/mode and listen, then
        # rename into place: clients never see a socket that is not yet
        # listening or that has the wrong permissions.
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(tmp)
        finally:
            os.umask(old)
        if self.cfg.socket_group:
            try:
                os.chown(tmp, -1, grp.getgrnam(self.cfg.socket_group).gr_gid)
            except (KeyError, PermissionError):
                log.warning("could not set socket group %s", self.cfg.socket_group)
        os.chmod(tmp, self.cfg.socket_mode)
        sock.listen(64)
        os.rename(tmp, path)
        return sock

    def serve_forever(self) -> None:
        self.listener = self.open_listener()
        self.listener.settimeout(0.5)
        log.info("qosd %s ready", __version__)
        while not self._stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            if not self.conn_sem.acquire(blocking=False):
                conn.close()  # connection limit reached
                continue
            threading.Thread(target=self.handle_connection, args=(conn,), daemon=True).start()
        self.listener.close()

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------
# Self-test (used by the trial-boot health check, OS-06)
# --------------------------------------------------------------------------
SELF_TEST_CIRCUIT = {
    "schema_version": 1,
    "n_qubits": 1,
    "operations": [{"op": "x", "qubits": [0]}, {"op": "measure", "qubits": [0]}],
}


def self_test(timeout: float = 30.0) -> bool:
    circuit = validate_circuit(SELF_TEST_CIRCUIT)
    counts = execute_in_worker(worker.encode_job(circuit, 16, 0), timeout)
    return counts == [["1", 16]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qosd", description="QuantumOS quantum service")
    ap.add_argument("--config", default=os.environ.get("QOSD_CONFIG", DEFAULT_CONFIG_PATH))
    ap.add_argument("--socket", help="override socket path")
    ap.add_argument("--self-test", action="store_true", help="run the deterministic self-test")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s", stream=sys.stderr)

    if args.self_test:
        try:
            ok = self_test()
        except QosError as exc:
            log.error("self-test failed: %s", exc.code)
            return 1
        log.info("self-test %s", "passed" if ok else "FAILED")
        return 0 if ok else 1

    cfg = ServiceConfig.load(args.config)
    if args.socket:
        cfg.socket_path = args.socket
    svc = QuantumService(cfg)
    signal.signal(signal.SIGTERM, lambda *_: svc.stop())
    signal.signal(signal.SIGINT, lambda *_: svc.stop())
    svc.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
