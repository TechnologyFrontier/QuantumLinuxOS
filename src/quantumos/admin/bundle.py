"""Offline update bundle: format, signing and verification.

A bundle is an uncompressed POSIX tar archive with exactly these regular
files (provisional format, QOS-IF 5章 leaves the final format open)::

    manifest.json   UTF-8 JSON, see below
    manifest.sig    raw 64-byte Ed25519 signature over manifest.json
    root.img        read-only root filesystem image for one slot
    vmlinuz         kernel for that root
    initrd.img      initramfs for that root

manifest.json::

    {"format": 1, "product_id": "quantumos", "architecture": "x86_64",
     "version": "0.2.0",
     "files": {"root.img":   {"sha256": "<hex>", "size": N},
               "vmlinuz":    {"sha256": "<hex>", "size": N},
               "initrd.img": {"sha256": "<hex>", "size": N}}}

The distributor's public key ships inside the OS image.  The signature is
checked *before* anything in the manifest is trusted, and every payload is
hashed against the manifest both before and after it is written.
"""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .. import strictjson
from .errors import AdminError

MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"
PAYLOADS = ("root.img", "vmlinuz", "initrd.img")
MEMBERS = frozenset((MANIFEST, SIGNATURE) + PAYLOADS)
MAX_MANIFEST_BYTES = 64 * 1024

_VERSION_RE = re.compile(r"(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8})){0,3}\Z", re.ASCII)
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CHUNK = 4 * 1024 * 1024


def parse_version(text: object) -> tuple[int, ...]:
    if not isinstance(text, str) or not _VERSION_RE.match(text):
        raise ValueError(f"invalid version: {text!r}")
    return tuple(int(x) for x in text.split("."))


@dataclass(frozen=True)
class Manifest:
    product_id: str
    architecture: str
    version: str
    files: dict  # name -> (sha256, size)
    raw: bytes


def parse_manifest(raw: bytes) -> Manifest:
    try:
        obj = strictjson.loads(raw)
    except strictjson.StrictJSONError as exc:
        raise AdminError("MANIFEST_INVALID", str(exc)) from exc
    if not isinstance(obj, dict) or set(obj) != {
        "format", "product_id", "architecture", "version", "files"
    }:
        raise AdminError("MANIFEST_INVALID", "unexpected manifest keys")
    if obj["format"] != 1 or isinstance(obj["format"], bool):
        raise AdminError("MANIFEST_INVALID", "unsupported manifest format")
    for key in ("product_id", "architecture", "version"):
        if not isinstance(obj[key], str) or not obj[key]:
            raise AdminError("MANIFEST_INVALID", f"{key} must be a non-empty string")
    try:
        parse_version(obj["version"])
    except ValueError as exc:
        raise AdminError("MANIFEST_INVALID", str(exc)) from exc
    files = obj["files"]
    if not isinstance(files, dict) or set(files) != set(PAYLOADS):
        raise AdminError("MANIFEST_INVALID", "files must list exactly " + ", ".join(PAYLOADS))
    out = {}
    for name, spec in files.items():
        if (
            not isinstance(spec, dict)
            or set(spec) != {"sha256", "size"}
            or not isinstance(spec["sha256"], str)
            or not _SHA_RE.match(spec["sha256"])
            or not isinstance(spec["size"], int)
            or isinstance(spec["size"], bool)
            or spec["size"] < 1
        ):
            raise AdminError("MANIFEST_INVALID", f"bad file entry: {name}")
        out[name] = (spec["sha256"], spec["size"])
    return Manifest(obj["product_id"], obj["architecture"], obj["version"], out, raw)


def load_public_key(path: str) -> Ed25519PublicKey:
    try:
        with open(path, "rb") as fh:
            key = serialization.load_pem_public_key(fh.read())
    except (OSError, ValueError) as exc:
        raise AdminError("KEY_UNAVAILABLE", f"cannot load public key: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise AdminError("KEY_UNAVAILABLE", "public key is not Ed25519")
    return key


class Bundle:
    """Read-only view of a bundle archive.  Members are never extracted to
    disk; they are streamed out of the tar file."""

    def __init__(self, path: str):
        try:
            self._tar = tarfile.open(path, mode="r:")
        except (OSError, tarfile.TarError) as exc:
            raise AdminError("BUNDLE_INVALID", f"cannot open bundle: {exc}") from exc
        self.members: dict[str, tarfile.TarInfo] = {}
        for m in self._tar.getmembers():
            if m.name not in MEMBERS or not m.isreg() or m.name in self.members:
                self.close()
                raise AdminError("BUNDLE_INVALID", f"unexpected bundle member: {m.name!r}")
            self.members[m.name] = m
        missing = MEMBERS - set(self.members)
        if missing:
            self.close()
            raise AdminError("BUNDLE_INVALID", "missing members: " + ", ".join(sorted(missing)))

    def close(self) -> None:
        self._tar.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read_small(self, name: str, limit: int) -> bytes:
        m = self.members[name]
        if m.size > limit:
            raise AdminError("BUNDLE_INVALID", f"{name} too large")
        fh = self._tar.extractfile(m)
        assert fh is not None
        return fh.read()

    def open(self, name: str) -> io.BufferedReader:
        fh = self._tar.extractfile(self.members[name])
        assert fh is not None
        return fh

    def size(self, name: str) -> int:
        return self.members[name].size

    def verified_manifest(self, public_key: Ed25519PublicKey) -> Manifest:
        raw = self.read_small(MANIFEST, MAX_MANIFEST_BYTES)
        sig = self.read_small(SIGNATURE, 64)
        try:
            public_key.verify(sig, raw)
        except InvalidSignature as exc:
            raise AdminError("SIGNATURE_INVALID", "manifest signature verification failed") from exc
        return parse_manifest(raw)

    def check_payload_hashes(self, manifest: Manifest) -> None:
        for name in PAYLOADS:
            sha, size = manifest.files[name]
            if self.size(name) != size:
                raise AdminError("HASH_MISMATCH", f"{name}: size differs from manifest")
            if sha256_stream(self.open(name), size) != sha:
                raise AdminError("HASH_MISMATCH", f"{name}: sha256 differs from manifest")


def sha256_stream(fh, size: int | None = None) -> str:
    h = hashlib.sha256()
    remaining = size
    while remaining is None or remaining > 0:
        chunk = fh.read(_CHUNK if remaining is None else min(_CHUNK, remaining))
        if not chunk:
            break
        h.update(chunk)
        if remaining is not None:
            remaining -= len(chunk)
    if remaining:
        raise AdminError("VERIFY_FAILED", "short read while hashing")
    return h.hexdigest()


# --------------------------------------------------------------------------
# Distributor side (used by tools/qos-bundle)
# --------------------------------------------------------------------------
def generate_keypair() -> tuple[bytes, bytes]:
    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return priv, pub


def create_bundle(
    out_path: str,
    private_key_pem: bytes,
    payload_paths: dict[str, str],
    product_id: str,
    architecture: str,
    version: str,
) -> dict:
    parse_version(version)
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("private key is not Ed25519")
    files = {}
    for name in PAYLOADS:
        with open(payload_paths[name], "rb") as fh:
            digest = sha256_stream(fh)
            size = fh.tell()
        files[name] = {"sha256": digest, "size": size}
    manifest = {
        "format": 1,
        "product_id": product_id,
        "architecture": architecture,
        "version": version,
        "files": files,
    }
    raw = (strictjson.dumps(manifest) + "\n").encode("utf-8")
    sig = key.sign(raw)
    with tarfile.open(out_path, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in ((MANIFEST, raw), (SIGNATURE, sig)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        for name in PAYLOADS:
            tar.add(payload_paths[name], arcname=name, recursive=False)
    return manifest
