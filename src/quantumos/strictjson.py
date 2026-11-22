"""Strict JSON decoding.

* rejects duplicate object keys at any depth,
* rejects ``NaN`` / ``Infinity`` / ``-Infinity`` and numbers that overflow to
  non-finite floats,
* requires valid UTF-8 input with no BOM,
* can report the exact UTF-8 byte span of each top-level member value, which
  the service uses to enforce the 4 MiB circuit limit on the bytes actually
  contained in the request (QOS-IF 3章).
"""

from __future__ import annotations

import json
import math
from typing import Any


class StrictJSONError(ValueError):
    pass


def _pairs_hook(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise StrictJSONError(f"duplicate key: {key!r}")
        obj[key] = value
    return obj


def _reject_constant(name):
    raise StrictJSONError(f"non-finite number: {name}")


def _parse_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise StrictJSONError("non-finite number")
    return value


_DECODER = json.JSONDecoder(
    object_pairs_hook=_pairs_hook,
    parse_constant=_reject_constant,
    parse_float=_parse_float,
)

_WS = " \t\n\r"


def _check_strings(value: Any) -> None:
    """Reject lone surrogates (JSON-escaped text that is not valid Unicode)."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            try:
                v.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise StrictJSONError("invalid unicode escape") from exc
        elif isinstance(v, dict):
            for k, item in v.items():
                stack.append(k)
                stack.append(item)
        elif isinstance(v, list):
            stack.extend(v)


def decode_bytes(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        raise StrictJSONError("byte order mark is not allowed")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise StrictJSONError("invalid UTF-8") from exc


def loads(data: bytes | str) -> Any:
    """Parse one complete JSON document strictly."""
    text = decode_bytes(data) if isinstance(data, (bytes, bytearray)) else data
    try:
        value = _DECODER.decode(text)
    except StrictJSONError:
        raise
    except (ValueError, RecursionError) as exc:
        raise StrictJSONError(f"malformed JSON: {exc}") from exc
    _check_strings(value)
    return value


def _skip_ws(text: str, idx: int) -> int:
    n = len(text)
    while idx < n and text[idx] in _WS:
        idx += 1
    return idx


def loads_object_with_spans(data: bytes) -> tuple[dict, dict[str, int]]:
    """Parse a top-level JSON object and return ``(obj, sizes)``.

    ``sizes`` maps each top-level key to the UTF-8 byte length of the raw text
    of its value as it appeared in ``data``.
    If the document is not a JSON object, ``(value, {})`` is returned.
    """
    text = decode_bytes(data)
    idx = _skip_ws(text, 0)
    if idx >= len(text) or text[idx] != "{":
        return loads(text), {}
    obj: dict = {}
    sizes: dict[str, int] = {}
    try:
        idx = _skip_ws(text, idx + 1)
        if idx < len(text) and text[idx] == "}":
            idx += 1
        else:
            while True:
                if idx >= len(text) or text[idx] != '"':
                    raise StrictJSONError("expected object key")
                key, idx = json.decoder.scanstring(text, idx + 1, True)
                idx = _skip_ws(text, idx)
                if idx >= len(text) or text[idx] != ":":
                    raise StrictJSONError("expected ':'")
                idx = _skip_ws(text, idx + 1)
                start = idx
                value, idx = _DECODER.raw_decode(text, idx)
                if key in obj:
                    raise StrictJSONError(f"duplicate key: {key!r}")
                obj[key] = value
                sizes[key] = len(text[start:idx].encode("utf-8", "surrogatepass"))
                idx = _skip_ws(text, idx)
                if idx < len(text) and text[idx] == ",":
                    idx = _skip_ws(text, idx + 1)
                    continue
                if idx < len(text) and text[idx] == "}":
                    idx += 1
                    break
                raise StrictJSONError("expected ',' or '}'")
    except StrictJSONError:
        raise
    except (ValueError, RecursionError) as exc:
        raise StrictJSONError(f"malformed JSON: {exc}") from exc
    if _skip_ws(text, idx) != len(text):
        raise StrictJSONError("trailing data after JSON document")
    _check_strings(obj)
    return obj, sizes


def dumps(value: Any) -> str:
    """Compact, deterministic single-line JSON."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
