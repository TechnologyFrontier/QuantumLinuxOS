"""Static checks for the initramfs scripts (image/initramfs).

The initramfs is built without busybox, so its ``mount`` is klibc's, which
stops option parsing at the first operand: every option must come before the
device and mount point.  A misplaced ``-o`` makes mount fail at boot, and
quantumos-state then drops to the initramfs shell.
"""

import pathlib
import re
import shlex

INITRAMFS = pathlib.Path(__file__).resolve().parent.parent / "image" / "initramfs"


def _mount_commands(text):
    """Yield the argument lists of ``mount`` invocations (line continuations joined)."""
    joined = re.sub(r"\\\n\s*", " ", text)
    for line in joined.splitlines():
        # only command positions: line start, optionally after "if" / "if !"
        m = re.match(r"\s*(?:if\s+(?:!\s*)?)?mount\s+(.*?)\s*(?:;\s*then)?\s*$", line)
        if m:
            yield shlex.split(m.group(1).replace("$", "_"))


def test_mount_options_precede_operands():
    found = 0
    for script in sorted(INITRAMFS.iterdir()):
        for args in _mount_commands(script.read_text()):
            found += 1
            seen_operand = False
            skip = False
            for arg in args:
                if skip:
                    skip = False
                    continue
                if arg.startswith("-"):
                    assert not seen_operand, f"{script.name}: option {arg!r} after operand in mount {args}"
                    skip = arg in ("-t", "-o")
                else:
                    seen_operand = True
            operands = [a for i, a in enumerate(args)
                        if not a.startswith("-") and (i == 0 or args[i - 1] not in ("-t", "-o"))]
            assert len(operands) == 2, f"{script.name}: expected device and mount point in mount {args}"
    assert found >= 2  # state partition and /etc overlay
