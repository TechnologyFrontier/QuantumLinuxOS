"""Update manager logic and the pkexec helper (QOS-DT 3.2, T-DT-04)."""

import json
import subprocess

from quantumos.desktop import update_model as um

STATUS = {"confirmed_slot": "a", "trial_slot": None, "state": "confirmed", "last_failure": None,
          "booted_slot": "a", "confirmed_version": "0.1.0", "trial_version": None}


def test_parse_status_and_texts():
    st = um.parse_status(json.dumps(STATUS) + "\n")
    assert st.can_apply and not st.needs_reboot
    assert st.state_text.startswith("確定済み")
    st = um.parse_status(json.dumps({**STATUS, "state": "trial_reserved", "trial_slot": "b",
                                     "result": "trial_reserved"}))
    assert st.needs_reboot and not st.can_apply
    st = um.parse_status(json.dumps({**STATUS, "state": "rolled_back",
                                     "last_failure": "HEALTH_CHECK_FAILED"}))
    assert st.can_apply and "健全性確認" in um.describe_failure(st.last_failure)
    assert um.describe_failure(None) == "なし"
    assert "PRODUCT_MISMATCH" in um.describe_failure("PRODUCT_MISMATCH")


def test_interpret_pkexec_outcomes():
    assert um.interpret("status", 126, "", "") is None
    f = um.interpret("status", 127, "", "")
    assert f.code == "PERMISSION_DENIED"
    f = um.interpret("apply", 1, "", '{"error":{"code":"PRODUCT_MISMATCH","message":"x"}}\n')
    assert f.code == "PRODUCT_MISMATCH" and "この OS（版）向け" in f.text
    f = um.interpret("apply", 1, "", "error: SIGNATURE_INVALID: bad signature\n")
    assert f.code == "SIGNATURE_INVALID"
    assert um.interpret("status", 0, "not json", "").code == "HELPER_FAILED"


def test_run_uses_the_fixed_helper(tmp_path):
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, json.dumps(STATUS), "")

    assert um.run(um.status_argv(), "status", runner).state == "confirmed"
    assert seen["argv"] == ["/usr/bin/pkexec", "/usr/libexec/quantumos/qos-update-helper", "status"]
    assert um.apply_argv("rel/b.qosb")[3].startswith("/")


def test_helper_only_runs_qos_admin_with_fixed_arguments(tmp_path, capsys):
    calls = []
    execv = lambda path, argv: calls.append((path, argv))  # noqa: E731
    assert um.helper_main(["status"], execv) == 0
    bundle = tmp_path / "u.qosb"
    bundle.write_bytes(b"x")
    assert um.helper_main(["apply", str(bundle)], execv) == 0
    assert calls == [
        ("/usr/bin/qos-admin", ["/usr/bin/qos-admin", "--json", "update-status"]),
        ("/usr/bin/qos-admin", ["/usr/bin/qos-admin", "--json", "update", str(bundle)]),
    ]
    assert um.helper_main(["apply", "relative.qosb"], execv) == 1
    assert um.helper_main(["apply", str(tmp_path / "missing")], execv) == 1
    assert um.helper_main(["health-check"], execv) == 2
    assert um.helper_main(["status", "--config=/tmp/x"], execv) == 2
    assert len(calls) == 2
