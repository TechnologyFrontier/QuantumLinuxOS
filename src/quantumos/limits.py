"""Provisional limits from QOS-BD 3章 / QOS-FD 2章 / QOS-IF 3章."""

MIB = 1024 * 1024

# Circuit structure (circuit JSON v1)
STRUCT_MIN_QUBITS = 1
STRUCT_MAX_QUBITS = 64
MAX_OPERATIONS = 10_000  # measurement included
MAX_CIRCUIT_BYTES = 4 * MIB  # UTF-8

# Execution backend
BACKEND_MAX_QUBITS = 20
SHOTS_MIN = 1
SHOTS_MAX = 100_000
SHOTS_DEFAULT = 1_024
SEED_MAX = 2**64 - 1

# IPC framing
FRAME_MIN_BYTES = 1
FRAME_MAX_BYTES = 8 * MIB

# Service resources (initial values, to be tuned after evaluation)
EXECUTION_TIMEOUT_SEC = 120.0
SERVICE_MEMORY_MAX = "512M"  # enforced by systemd MemoryMax=
