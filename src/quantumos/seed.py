"""64-bit seed handling (QOS-FD 2.1, QOS-IF 4章).

A seed is a canonical ASCII decimal string: 1–20 digits, no leading zero
except ``"0"`` itself, value 0 … 2**64-1.  JSON numbers, ``null``, signs,
whitespace and exponents are rejected.
"""

from __future__ import annotations

import re
import secrets

from .limits import SEED_MAX

_SEED_RE = re.compile(r"(?:0|[1-9][0-9]{0,19})\Z", re.ASCII)


def parse_seed(value: object) -> int:
    """Return the integer seed or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError("seed must be a decimal string")
    if not _SEED_RE.match(value):
        raise ValueError("seed must be a canonical ASCII decimal string")
    n = int(value)
    if n > SEED_MAX:
        raise ValueError("seed out of range")
    return n


def generate_seed() -> int:
    """Draw a seed from the OS random source (getrandom(2) via ``secrets``)."""
    return secrets.randbits(64)


def format_seed(n: int) -> str:
    return str(n)
