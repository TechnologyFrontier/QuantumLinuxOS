"""量子回路スタジオ – GTK4/libadwaita front end (QOS-DT DT-02).

All circuit rules and the qosctl contract live in ``circuit_model`` and
``qosctl_client``; this module only builds widgets and moves work off the UI
thread.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ..limits import SHOTS_DEFAULT, SHOTS_MAX, SHOTS_MIN, STRUCT_MAX_QUBITS  # noqa: E402
from ..seed import parse_seed  # noqa: E402
from . import circuit_model as cm  # noqa: E402
from . import qosctl_client as qc  # noqa: E402

APP_ID = "org.quantumos.Studio"

STATUS_INTERVAL_SEC = 15
STATE_TEXT = {"ready": "待機中（ready）", "busy": "計算中（busy）"}


def _rgba(widget, alpha=1.0):
    color = widget.get_color()
    return color.red, color.green, color.blue, alpha * color.alpha


def _accent():
    return 0.21, 0.52, 0.89


class CircuitView(Gtk.DrawingArea):
    """Circuit diagram drawn with cairo from ``CircuitModel.layout()``."""

    COL_W, ROW_H, LEFT, TOP, BOX = 56, 48, 56, 28, 34

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.selected: int | None = None
        self.set_draw_func(self._draw)
        click = Gtk.GestureClick()
        click.connect("pressed", self._clicked)
        self.add_controller(click)

    def refresh(self):
        m = self.window.model
        cols = len(m.layout()) + 1
        self.set_content_width(self.LEFT + self.COL_W * (cols + 1))
        self.set_content_height(self.TOP * 2 + self.ROW_H * m.n_qubits)
        self.queue_draw()

    def _cx(self, col):
        return self.LEFT + self.COL_W * col + self.COL_W / 2

    def _cy(self, q):
        return self.TOP + self.ROW_H * q + self.ROW_H / 2

    def _clicked(self, _gesture, _n, x, y):
        m = self.window.model
        for col, ops in enumerate(m.layout()):
            for i in ops:
                qs = m.operations[i].qubits
                if abs(x - self._cx(col)) < self.BOX / 2 and \
                        min(map(self._cy, qs)) - self.BOX / 2 <= y <= max(map(self._cy, qs)) + self.BOX / 2:
                    self.window.select_operation(i)
                    return

    def _draw(self, _area, cr, width, _height):
        m = self.window.model
        fg = _rgba(self)
        cr.select_font_face("Sans")
        columns = m.layout()
        end_x = self._cx(len(columns)) + self.COL_W / 2
        cr.set_line_width(1.2)
        for q in range(m.n_qubits):
            y = self._cy(q)
            cr.set_source_rgba(*fg)
            cr.set_font_size(13)
            cr.move_to(8, y + 5)
            cr.show_text(f"q{q}")
            cr.set_source_rgba(*_rgba(self, 0.55))
            cr.move_to(self.LEFT - 10, y)
            cr.line_to(end_x, y)
            cr.stroke()
        for col, ops in enumerate(columns):
            for i in ops:
                self._draw_op(cr, m.operations[i], self._cx(col), i == self.selected, fg)
        # terminal measurement
        x = self._cx(len(columns))
        for q in m.measured:
            self._box(cr, x, self._cy(q), "M", False, fg, fill=(0.55, 0.55, 0.55, 0.25))

    def _box(self, cr, x, y, text, selected, fg, fill=None):
        b = self.BOX
        r, g, bl = _accent()
        cr.rectangle(x - b / 2, y - b / 2, b, b)
        cr.set_source_rgba(*(fill or (r, g, bl, 0.9)))
        cr.fill_preserve()
        cr.set_source_rgba(*(fg if not selected else (1.0, 0.65, 0.0, 1.0)))
        cr.set_line_width(3 if selected else 1)
        cr.stroke()
        cr.set_source_rgba(*(fg if fill else (1, 1, 1, 1)))
        cr.set_font_size(13 if len(text) <= 2 else 10)
        ext = cr.text_extents(text)
        cr.move_to(x - ext.width / 2 - ext.x_bearing, y - ext.height / 2 - ext.y_bearing)
        cr.show_text(text)

    def _draw_op(self, cr, op, x, selected, fg):
        if op.op == "cx":
            c, t = op.qubits
            yc, yt = self._cy(c), self._cy(t)
            cr.set_source_rgba(*((1.0, 0.65, 0.0, 1.0) if selected else (*_accent(), 1.0)))
            cr.set_line_width(2)
            cr.move_to(x, yc)
            cr.line_to(x, yt + (11 if yt > yc else -11))
            cr.stroke()
            cr.arc(x, yc, 6, 0, 2 * math.pi)
            cr.fill()
            cr.arc(x, yt, 11, 0, 2 * math.pi)
            cr.stroke()
            cr.move_to(x - 11, yt)
            cr.line_to(x + 11, yt)
            cr.move_to(x, yt - 11)
            cr.line_to(x, yt + 11)
            cr.stroke()
            return
        self._box(cr, x, self._cy(op.qubits[0]), op.op.upper(), selected, fg)


class Histogram(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.rows: list[tuple[str, int, float]] = []
        self.set_content_height(220)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def show(self, rows):
        self.rows = rows
        self.queue_draw()

    def _draw(self, _a, cr, width, height):
        if not self.rows:
            return
        fg = _rgba(self)
        top, bottom = 18, 26
        n = len(self.rows)
        slot = width / n
        bar = max(4.0, min(64.0, slot * 0.7))
        peak = max(r for _, _, r in self.rows) or 1.0
        cr.set_font_size(11)
        for i, (key, count, ratio) in enumerate(self.rows):
            x = slot * i + slot / 2
            h = (height - top - bottom) * ratio / peak
            cr.set_source_rgba(*_accent(), 0.9)
            cr.rectangle(x - bar / 2, height - bottom - h, bar, h)
            cr.fill()
            cr.set_source_rgba(*fg)
            if n <= 32:
                for text, y in ((key, height - 8), (f"{ratio * 100:.1f}%", height - bottom - h - 5)):
                    ext = cr.text_extents(text)
                    cr.move_to(x - ext.width / 2, y)
                    cr.show_text(text)


class StudioWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="量子回路スタジオ")
        self.set_default_size(1200, 780)
        self.client = qc.QosctlClient()
        self.model = cm.sample("ベル状態（2 量子ビット）")
        self.path: str | None = None
        self.dirty = False
        self.invocation: qc.Invocation | None = None
        self.result: qc.RunResult | None = None

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="量子回路スタジオ", subtitle="量子サービス: 確認中…")
        header.set_title_widget(self.title)
        for icon, tip, cb in (("document-open-symbolic", "開く", self.on_open),
                              ("document-save-symbolic", "保存", self.on_save)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.connect("clicked", cb)
            header.pack_start(b)
        samples = Gio.Menu()
        for name in cm.SAMPLES:
            samples.append(name, f"win.sample::{name}")
        act = Gio.SimpleAction.new("sample", GLib.VariantType.new("s"))
        act.connect("activate", lambda _a, v: self.load_model(cm.sample(v.get_string()), None))
        self.add_action(act)
        header.pack_start(Gtk.MenuButton(label="サンプル", menu_model=samples))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="量子サービスの状態を確認")
        refresh.connect("clicked", lambda _b: self.refresh_status())
        header.pack_end(refresh)
        view.add_top_bar(header)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=430)
        paned.set_start_child(self._editor_panel())
        paned.set_end_child(self._main_panel())
        view.set_content(paned)
        self.reload()
        self.refresh_status()
        GLib.timeout_add_seconds(STATUS_INTERVAL_SEC, self._periodic_status)

    def _periodic_status(self):
        if self.invocation is None and self.is_visible():
            self.refresh_status()
        return True

    # --- layout ------------------------------------------------------------
    def _editor_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        group = Adw.PreferencesGroup(title="回路")
        self.qubits_row = Adw.SpinRow.new_with_range(1, STRUCT_MAX_QUBITS, 1)
        self.qubits_row.set_title("量子ビット数")
        self.qubits_row.connect("notify::value", self.on_qubits_changed)
        group.add(self.qubits_row)
        box.append(group)

        add = Adw.PreferencesGroup(title="ゲートを追加")
        self.gate_row = Adw.ComboRow(title="ゲート",
                                     model=Gtk.StringList.new([cm.GATE_LABELS[g] for g in cm.EDITABLE_GATES]))
        self.gate_row.connect("notify::selected", lambda *_: self._update_gate_inputs())
        add.add(self.gate_row)
        self.q0_row = Adw.SpinRow.new_with_range(0, 0, 1)
        self.q0_row.set_title("量子ビット")
        self.q1_row = Adw.SpinRow.new_with_range(0, 0, 1)
        self.q1_row.set_title("標的の量子ビット")
        self.theta_row = Adw.EntryRow(title="角度 θ（例: pi/2、0.5）")
        self.theta_row.set_text("pi/2")
        for r in (self.q0_row, self.q1_row, self.theta_row):
            add.add(r)
        add_btn = Gtk.Button(label="追加", css_classes=["suggested-action"], margin_top=6)
        add_btn.connect("clicked", self.on_add_gate)
        box.append(add)
        box.append(add_btn)

        ops = Adw.PreferencesGroup(title="操作の順序", description="図のゲートをクリックしても選べます")
        self.ops_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.SINGLE)
        self.ops_list.connect("row-selected", self.on_row_selected)
        ops.add(self.ops_list)
        tools = Gtk.Box(spacing=6, margin_top=6, halign=Gtk.Align.END)
        for icon, tip, cb in (("go-up-symbolic", "前へ", lambda _b: self.on_move(-1)),
                              ("go-down-symbolic", "後へ", lambda _b: self.on_move(1)),
                              ("user-trash-symbolic", "削除", self.on_remove)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.connect("clicked", cb)
            tools.append(b)
        box.append(ops)
        box.append(tools)

        meas = Adw.PreferencesGroup(title="測定する量子ビット（最後に一括測定）")
        self.measure_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8)
        meas.add(self.measure_box)
        box.append(meas)
        return Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _main_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.circuit_view = CircuitView(self)
        box.append(Gtk.Frame(child=Gtk.ScrolledWindow(child=self.circuit_view, min_content_height=160,
                                                      vexpand=False, propagate_natural_height=True)))
        self.problems = Gtk.Label(xalign=0, wrap=True, css_classes=["caption"])
        box.append(self.problems)

        run = Gtk.Box(spacing=12)
        run.append(Gtk.Label(label="観測回数"))
        self.shots = Gtk.SpinButton.new_with_range(SHOTS_MIN, SHOTS_MAX, 1)
        self.shots.set_value(SHOTS_DEFAULT)
        run.append(self.shots)
        run.append(Gtk.Label(label="シード（省略可）"))
        self.seed = Gtk.Entry(placeholder_text="自動", width_chars=22)
        run.append(self.seed)
        self.validate_btn = Gtk.Button(label="検証")
        self.validate_btn.connect("clicked", self.on_validate)
        self.run_btn = Gtk.Button(label="実行", css_classes=["suggested-action"])
        self.run_btn.connect("clicked", self.on_run)
        self.stop_btn = Gtk.Button(label="停止", css_classes=["destructive-action"], sensitive=False)
        self.stop_btn.connect("clicked", self.on_stop)
        self.spinner = Gtk.Spinner()
        for w in (self.validate_btn, self.run_btn, self.stop_btn, self.spinner):
            run.append(w)
        box.append(run)

        self.banner = Adw.Banner()
        box.append(self.banner)

        results = Adw.PreferencesGroup(title="結果")
        actions = Gtk.Box(spacing=6)
        self.rerun_btn = Gtk.Button(label="同じシードで再実行", sensitive=False)
        self.rerun_btn.connect("clicked", self.on_rerun)
        self.save_result_btn = Gtk.Button(label="結果を保存…", sensitive=False)
        self.save_result_btn.connect("clicked", self.on_save_result)
        actions.append(self.rerun_btn)
        actions.append(self.save_result_btn)
        results.set_header_suffix(actions)
        self.meta = Gtk.Label(xalign=0, selectable=True, wrap=True, css_classes=["dim-label"])
        results.add(self.meta)
        self.hist = Histogram()
        results.add(self.hist)
        self.table = Gtk.Grid(column_spacing=24, row_spacing=4, margin_top=6)
        results.add(Gtk.ScrolledWindow(child=self.table, min_content_height=120, vexpand=True))
        box.append(results)
        return Gtk.ScrolledWindow(child=box)

    # --- model <-> widgets ---------------------------------------------------
    def reload(self, select: int | None = None):
        m = self.model
        self._syncing = True
        self.qubits_row.set_value(m.n_qubits)
        for r in (self.q0_row, self.q1_row):
            r.get_adjustment().set_upper(m.n_qubits - 1)
        self.q1_row.set_value(min(1, m.n_qubits - 1))
        self._syncing = False
        self._update_gate_inputs()

        while (row := self.ops_list.get_row_at_index(0)) is not None:
            self.ops_list.remove(row)
        for i, op in enumerate(m.operations):
            self.ops_list.append(Gtk.Label(label=f"{i + 1}. {op.describe()}", xalign=0,
                                           margin_top=6, margin_bottom=6, margin_start=8))
        child = self.measure_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.measure_box.remove(child)
            child = nxt
        for q in range(m.n_qubits):
            cb = Gtk.CheckButton(label=f"q{q}", active=q in m.measured)
            cb.connect("toggled", self.on_measure_toggled, q)
            self.measure_box.append(cb)
        if select is not None and 0 <= select < len(m.operations):
            self.ops_list.select_row(self.ops_list.get_row_at_index(select))
        else:
            self.circuit_view.selected = None
        self.circuit_view.refresh()
        problems = m.problems()
        self.problems.set_label("構造チェック: 問題はありません" if not problems else
                                "構造チェック:\n" + "\n".join(f"・{p}" for p in problems))
        self.problems.set_css_classes(["caption"] + (["error"] if problems else ["success"]))
        name = os.path.basename(self.path) if self.path else "無題"
        self.set_title(f"{'*' if self.dirty else ''}{name} – 量子回路スタジオ")

    def _update_gate_inputs(self):
        gate = cm.EDITABLE_GATES[self.gate_row.get_selected()]
        self.q1_row.set_visible(gate == "cx")
        self.q0_row.set_title("制御の量子ビット" if gate == "cx" else "量子ビット")
        self.theta_row.set_visible(gate in ("rx", "rz"))

    def changed(self, select=None):
        self.dirty = True
        self.reload(select)

    def load_model(self, model, path):
        self.model, self.path, self.dirty = model, path, False
        self.reload()

    def select_operation(self, i):
        self.ops_list.select_row(self.ops_list.get_row_at_index(i))

    # --- editing handlers ------------------------------------------------------
    def on_qubits_changed(self, row, _p):
        if self._syncing:
            return
        n = int(row.get_value())
        if n != self.model.n_qubits:
            self.model.set_qubits(n)
            self.changed()

    def on_add_gate(self, _b):
        gate = cm.EDITABLE_GATES[self.gate_row.get_selected()]
        q0 = int(self.q0_row.get_value())
        qubits = (q0, int(self.q1_row.get_value())) if gate == "cx" else (q0,)
        theta = None
        if gate in ("rx", "rz"):
            try:
                theta = cm.parse_theta(self.theta_row.get_text())
            except ValueError as exc:
                self.toast(str(exc))
                return
        sel = self.ops_list.get_selected_row()
        index = sel.get_index() + 1 if sel else None
        self.model.add(gate, qubits, theta, index)
        self.changed(index if index is not None else len(self.model.operations) - 1)

    def on_row_selected(self, _lb, row):
        self.circuit_view.selected = row.get_index() if row else None
        self.circuit_view.queue_draw()

    def on_move(self, delta):
        row = self.ops_list.get_selected_row()
        if row:
            self.changed(self.model.move(row.get_index(), delta))

    def on_remove(self, _b):
        row = self.ops_list.get_selected_row()
        if row:
            i = row.get_index()
            self.model.remove(i)
            self.changed(min(i, len(self.model.operations) - 1))

    def on_measure_toggled(self, cb, q):
        measured = set(self.model.measured)
        (measured.add if cb.get_active() else measured.discard)(q)
        self.model.measured = sorted(measured)
        self.changed()

    # --- files -------------------------------------------------------------
    def _json_filter(self):
        f = Gtk.FileFilter(name="回路 JSON")
        f.add_pattern("*.json")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        return store

    def on_open(self, _b):
        dlg = Gtk.FileDialog(title="回路を開く", filters=self._json_filter())
        dlg.open(self, None, self._opened)

    def _opened(self, dlg, res):
        try:
            gfile = dlg.open_finish(res)
        except GLib.Error:
            return
        path = gfile.get_path()
        try:
            with open(path, "rb") as fh:
                self.load_model(cm.CircuitModel.from_json_text(fh.read()), path)
        except (OSError, ValueError) as exc:
            self.toast(f"開けません: {exc}")

    def on_save(self, _b):
        if self.path:
            self._write_circuit(self.path)
            return
        dlg = Gtk.FileDialog(title="回路を保存", initial_name="circuit.json", filters=self._json_filter())
        dlg.save(self, None, self._save_chosen)

    def _save_chosen(self, dlg, res):
        try:
            gfile = dlg.save_finish(res)
        except GLib.Error:
            return
        self._write_circuit(gfile.get_path())

    def _write_circuit(self, path):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.model.to_json_text())
        except OSError as exc:
            self.toast(f"保存できません: {exc.strerror}")
            return
        self.path, self.dirty = path, False
        self.reload()
        self.toast("保存しました")

    def on_save_result(self, _b):
        if not self.result:
            return
        dlg = Gtk.FileDialog(title="結果を保存", initial_name="result.json")
        dlg.save(self, None, self._result_chosen)

    def _result_chosen(self, dlg, res):
        try:
            path = dlg.save_finish(res).get_path()
        except GLib.Error:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.result.raw_line + "\n")  # qosctl output, seed kept as a string
            self.toast("結果を保存しました")
        except OSError as exc:
            self.toast(f"保存できません: {exc.strerror}")

    # --- service ----------------------------------------------------------
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def _background(self, work, done):
        def thread():
            value = work()
            GLib.idle_add(lambda: (done(value), False)[1])
        threading.Thread(target=thread, daemon=True).start()

    def refresh_status(self):
        def done(v):
            if isinstance(v, qc.Failure):
                self.title.set_subtitle(f"量子サービス: 利用できません（{v.code}）")
            else:
                self.title.set_subtitle(f"量子サービス: {STATE_TEXT.get(v, v)}")
        self._background(self.client.status, done)

    def _circuit_file(self):
        """A private temporary copy of the current circuit for qosctl."""
        base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        fd, path = tempfile.mkstemp(prefix="qos-studio-", suffix=".json", dir=base)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self.model.to_json_text())
        return path

    def _show_failure(self, f: qc.Failure, what: str):
        self.banner.set_title(GLib.markup_escape_text(
            f"{what}に失敗しました（{f.code}・{f.category}）: {f.message} — {f.guidance}"))
        self.banner.set_revealed(True)

    def on_validate(self, _b):
        path = self._circuit_file()

        def work():
            try:
                return self.client.validate(path)
            finally:
                os.unlink(path)

        def done(v):
            if isinstance(v, qc.Failure):
                self._show_failure(v, "検証")
            else:
                self.banner.set_revealed(False)
                self.toast("量子サービスの検証: 有効な回路です")
        self._background(work, done)

    def _set_running(self, running):
        self.run_btn.set_sensitive(not running)
        self.validate_btn.set_sensitive(not running)
        self.stop_btn.set_sensitive(running)
        self.spinner.set_spinning(running)
        if running:
            self.banner.set_revealed(False)
            self.title.set_subtitle("量子サービス: 実行中…")

    def on_run(self, _b, seed: str | None = None, shots: int | None = None):
        if self.invocation is not None:
            return
        seed = seed if seed is not None else (self.seed.get_text().strip() or None)
        if seed is not None:
            try:
                parse_seed(seed)
            except ValueError:
                self.toast("シードは 0〜18446744073709551615 の 10 進数で入力してください（先頭の 0 は不可）")
                return
        shots = shots or int(self.shots.get_value())
        path = self._circuit_file()
        try:
            inv = self.client.start_run(path, shots, seed)
        except OSError as exc:
            os.unlink(path)
            self._show_failure(qc.Failure("SERVICE_UNAVAILABLE", str(exc)), "実行")
            return
        self.invocation = inv
        self._set_running(True)

        def work():
            try:
                return self.client.interpret(inv, *inv.wait(), "run")
            finally:
                os.unlink(path)

        def done(v):
            if self.invocation is inv:
                self.invocation = None
            self._set_running(False)
            if inv.stopped:
                self.toast("停止しました。停止後の出力は採用しません")
                self.refresh_status()
                return
            if isinstance(v, qc.Failure):
                self._show_failure(v, "実行")  # the previous good result stays on screen
            else:
                self.banner.set_revealed(False)
                self.show_result(v)
            self.refresh_status()
        self._background(work, done)

    def on_stop(self, _b):
        if self.invocation:
            self.invocation.stop()

    def on_rerun(self, _b):
        if self.result:
            self.on_run(None, seed=self.result.seed, shots=self.result.shots)

    def show_result(self, r: qc.RunResult):
        self.result = r
        rows = r.rows()
        self.hist.show(rows)
        child = self.table.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.table.remove(child)
            child = nxt
        for col, text in enumerate(("観測値", "回数", "比率")):
            self.table.attach(Gtk.Label(label=text, xalign=0, css_classes=["heading"]), col, 0, 1, 1)
        for i, (key, count, ratio) in enumerate(rows, start=1):
            for col, text in enumerate((key, f"{count:,}", f"{ratio * 100:.2f}%")):
                self.table.attach(Gtk.Label(label=text, xalign=0, selectable=True,
                                            css_classes=["monospace"]), col, i, 1, 1)
        total = sum(c for _, c, _ in rows)
        self.meta.set_label(
            f"合計 {total:,} / shots {r.shots:,}・測定 {', '.join(f'q{q}' for q in r.measured_qubits)}・"
            f"seed {r.seed}・backend {r.backend_id}・service {r.service_version}\n"
            "観測値の文字列は左から測定した量子ビットの順です。結果は保存しない限り残りません。")
        self.rerun_btn.set_sensitive(True)
        self.save_result_btn.set_sensitive(True)


class StudioApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def do_activate(self):
        (self.get_active_window() or StudioWindow(self)).present()

    def do_open(self, files, _n, _hint):
        win = self.get_active_window() or StudioWindow(self)
        path = files[0].get_path()
        try:
            with open(path, "rb") as fh:
                win.load_model(cm.CircuitModel.from_json_text(fh.read()), path)
        except (OSError, ValueError) as exc:
            win.toast(f"開けません: {exc}")
        win.present()


def main(argv=None) -> int:
    return StudioApp().run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
