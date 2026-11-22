"""``qos-bundle`` – distributor-side tool (not installed in the OS image).

    qos-bundle keygen PRIVATE_KEY PUBLIC_KEY
    qos-bundle create --key PRIVATE_KEY --version V --root root.img \\
                      --kernel vmlinuz --initrd initrd.img OUT.qosb
    qos-bundle verify --pubkey PUBLIC_KEY BUNDLE
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import bundle as b
from .errors import AdminError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qos-bundle")
    sub = ap.add_subparsers(dest="command", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("private_key")
    k.add_argument("public_key")
    c = sub.add_parser("create")
    c.add_argument("--key", required=True)
    c.add_argument("--version", required=True)
    c.add_argument("--product-id", default="quantumos")
    c.add_argument("--architecture", default="x86_64")
    c.add_argument("--root", required=True)
    c.add_argument("--kernel", required=True)
    c.add_argument("--initrd", required=True)
    c.add_argument("out")
    v = sub.add_parser("verify")
    v.add_argument("--pubkey", required=True)
    v.add_argument("bundle")
    args = ap.parse_args(argv)

    if args.command == "keygen":
        priv, pub = b.generate_keypair()
        fd = os.open(args.private_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(priv)
        with open(args.public_key, "wb") as fh:
            fh.write(pub)
        return 0
    if args.command == "create":
        with open(args.key, "rb") as fh:
            key = fh.read()
        man = b.create_bundle(
            args.out, key,
            {"root.img": args.root, "vmlinuz": args.kernel, "initrd.img": args.initrd},
            args.product_id, args.architecture, args.version,
        )
        print(json.dumps(man, indent=2))
        return 0
    try:
        with b.Bundle(args.bundle) as bun:
            man = bun.verified_manifest(b.load_public_key(args.pubkey))
            bun.check_payload_hashes(man)
    except AdminError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    print(f"OK {man.product_id} {man.architecture} {man.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
