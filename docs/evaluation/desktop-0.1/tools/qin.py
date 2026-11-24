#!/usr/bin/env python3
"""Drive the VM's virtual USB tablet/keyboard through QMP (works under Wayland).

  qin.py click X Y [W H]     left click at screen pixel X,Y (screen W x H, default 1280x800)
  qin.py dclick X Y [W H]    double click
  qin.py type 'text'         type ASCII text
  qin.py key ctrl-l ret      press key combos (qcode names joined by '-')
"""
import json, os, socket, sys, time

QMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qmp.sock")


class Q:
    def __init__(self):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.connect(QMP)
        self.f = self.s.makefile("rw")
        self.f.readline()
        self.cmd("qmp_capabilities")

    def cmd(self, name, **args):
        self.f.write(json.dumps({"execute": name, "arguments": args} if args else {"execute": name}) + "\n")
        self.f.flush()
        while True:
            line = json.loads(self.f.readline())
            if "return" in line or "error" in line:
                if "error" in line:
                    print(line, file=sys.stderr)
                return line

    def move(self, x, y, w, h):
        ev = [{"type": "abs", "data": {"axis": "x", "value": int(x * 32767 / (w - 1))}},
              {"type": "abs", "data": {"axis": "y", "value": int(y * 32767 / (h - 1))}}]
        self.cmd("input-send-event", events=ev)

    def button(self, down):
        self.cmd("input-send-event", events=[{"type": "btn", "data": {"down": down, "button": "left"}}])

    def click(self, x, y, w=1280, h=800, n=1):
        self.move(x, y, w, h)
        time.sleep(0.3)
        for _ in range(n):
            self.button(True); time.sleep(0.08); self.button(False); time.sleep(0.12)

    def combo(self, spec):
        keys = [{"type": "qcode", "data": k} for k in spec.split("-")]
        self.cmd("send-key", keys=keys, **{"hold-time": 80})
        time.sleep(0.15)


SHIFTED = {'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
           '_': 'minus', '+': 'equal', '{': 'bracket_left', '}': 'bracket_right', ':': 'semicolon',
           '"': 'apostrophe', '<': 'comma', '>': 'dot', '?': 'slash', '~': 'grave_accent', '|': 'backslash'}
PLAIN = {' ': 'spc', '-': 'minus', '=': 'equal', '[': 'bracket_left', ']': 'bracket_right', ';': 'semicolon',
         "'": 'apostrophe', ',': 'comma', '.': 'dot', '/': 'slash', '`': 'grave_accent', '\\': 'backslash',
         '\n': 'ret'}


def type_text(q, text):
    for ch in text:
        if ch.isalpha() and ch.isascii():
            q.combo(("shift-" if ch.isupper() else "") + ch.lower())
        elif ch.isdigit():
            q.combo(ch)
        elif ch in PLAIN:
            q.combo(PLAIN[ch])
        elif ch in SHIFTED:
            q.combo("shift-" + SHIFTED[ch])
        else:
            raise SystemExit(f"cannot type {ch!r}")


if __name__ == "__main__":
    q = Q()
    op, args = sys.argv[1], sys.argv[2:]
    if op in ("click", "dclick"):
        nums = list(map(int, args))
        q.click(*nums, n=2 if op == "dclick" else 1)
    elif op == "type":
        type_text(q, args[0])
    elif op == "key":
        for k in args:
            q.combo(k)
