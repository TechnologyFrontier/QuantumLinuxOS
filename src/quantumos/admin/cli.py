"""``qos-admin`` – local administrator tool (QOS-IF 5章).

    qos-admin update BUNDLE      verify and write the inactive slot, reserve a trial
    qos-admin update-status      confirmed slot, trial slot, state, last failure
    qos-admin boot-assess        (boot-time unit) confirm or roll back a trial
    qos-admin health-check       run the trial health checks now (diagnosis)
    qos-admin service-status     quantum service liveness for the console

Machine-readable output (``--json``) is provisional: the final format is an
open item of QOS-IF 0.1.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from .. import __version__
from .config import AdminConfig
from .errors import AdminError
from . import update as upd

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _print_status(info: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(info, separators=(",", ":")))
        return
    print(f"confirmed slot : {info['confirmed_slot']} ({info['confirmed_version']})")
    trial = info["trial_slot"]
    print(f"trial slot     : {trial + ' (' + str(info['trial_version']) + ')' if trial else 'none'}")
    print(f"state          : {info['state']}")
    print(f"last failure   : {info['last_failure'] or 'none'}")
    print(f"booted slot    : {info['booted_slot'] or 'unknown'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qos-admin", description="QuantumOS administration")
    ap.add_argument("--version", action="version", version=f"qos-admin {__version__}")
    ap.add_argument("--config", help="configuration file (default $QOS_ADMIN_CONFIG)")
    ap.add_argument("--json", action="store_true", help="machine-readable output (provisional)")
    sub = ap.add_subparsers(dest="command", required=True)
    u = sub.add_parser("update", help="apply an offline update bundle to the inactive slot")
    u.add_argument("bundle")
    sub.add_parser("update-status", help="show the update state")
    sub.add_parser("boot-assess", help="confirm or roll back a trial boot (boot-time unit)")
    sub.add_parser("health-check", help="run the trial-boot health checks")
    sub.add_parser("service-status", help="quantum service liveness")
    args = ap.parse_args(argv)

    try:
        cfg = AdminConfig.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"error: CONFIG_INVALID: {exc}", file=sys.stderr)
        return EXIT_FAILED

    log = (lambda m: None) if args.json else (lambda m: print(m, file=sys.stderr))
    try:
        if args.command == "update":
            st = upd.update(cfg, args.bundle, log)
            info = upd.status(cfg)
            if args.json:
                print(json.dumps({"result": "trial_reserved", **info}, separators=(",", ":")))
            else:
                print(
                    f"slot {st.trial_slot} ({st.trial_version}) written and verified; "
                    "trial boot reserved. The system is NOT rebooted automatically."
                )
        elif args.command == "update-status":
            _print_status(upd.status(cfg), args.json)
        elif args.command == "boot-assess":
            st = upd.boot_assess(cfg, log)
            if args.json:
                print(json.dumps(upd.status(cfg), separators=(",", ":")))
            else:
                print(f"state: {st.state}")
        elif args.command == "health-check":
            code = upd.run_health_checks(cfg, log)
            if args.json:
                print(json.dumps({"healthy": code is None, "failure": code}))
            else:
                print("healthy" if code is None else f"unhealthy: {code}")
            return EXIT_OK if code is None else EXIT_FAILED
        elif args.command == "service-status":
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", "qosd.service"],
                    capture_output=True, text=True, timeout=10,
                )
                unit = r.stdout.strip() or "unknown"
            except (OSError, subprocess.SubprocessError):
                unit = "unknown"
            answering = upd.probe_socket(cfg.socket_path)
            if args.json:
                print(json.dumps({"unit": unit, "socket_answering": answering}))
            else:
                print(f"qosd.service : {unit}")
                print(f"socket       : {'answering' if answering else 'not answering'}")
            return EXIT_OK if answering else EXIT_FAILED
    except AdminError as exc:
        if args.json:
            print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=sys.stderr)
        else:
            print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
