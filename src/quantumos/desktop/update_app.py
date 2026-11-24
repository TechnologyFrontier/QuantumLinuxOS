"""量子OS 更新マネージャー – GTK4/libadwaita front end (QOS-DT DT-03)."""

from __future__ import annotations

import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import update_model as um  # noqa: E402

APP_ID = "org.quantumos.UpdateManager"


class Headline(Gtk.Box):
    def __init__(self):
        super().__init__(spacing=18, margin_bottom=6)
        self.icon = Gtk.Image(pixel_size=48, valign=Gtk.Align.CENTER)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER,
                        hexpand=True)
        self.title = Gtk.Label(xalign=0, wrap=True, css_classes=["title-2"])
        self.description = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        texts.append(self.title)
        texts.append(self.description)
        self.append(self.icon)
        self.append(texts)

    def set(self, icon, title, description):
        self.icon.set_from_icon_name(icon)
        self.title.set_label(title)
        self.description.set_label(description)


class UpdateWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="量子OS 更新マネージャー")
        self.set_default_size(720, 640)
        self.status: um.UpdateStatus | None = None
        self.busy = False

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        header = Adw.HeaderBar()
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="状態を再読み込み")
        self.refresh_btn.connect("clicked", lambda _b: self.refresh())
        header.pack_end(self.refresh_btn)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()
        view.set_content(page)

        self.banner_group = Adw.PreferencesGroup()
        self.headline = Headline()
        self.headline.set("system-software-update-symbolic", "更新状態を確認しています…",
                          "管理者の認証を求められた場合は、管理者のパスワードを入力してください。")
        # shown next to the headline while a trial boot waits for a reboot
        self.head_reboot_btn = Gtk.Button(label="再起動…", valign=Gtk.Align.CENTER, visible=False,
                                          css_classes=["destructive-action"])
        self.head_reboot_btn.connect("clicked", self.on_reboot)
        self.headline.append(self.head_reboot_btn)
        self.banner_group.add(self.headline)
        page.add(self.banner_group)

        g = Adw.PreferencesGroup(title="現在の状態",
                                 description="読み取り専用ルートの A/B スロットと試行起動の状況")
        self.rows = {}
        for key, title in (("version", "確定した版"), ("booted", "起動中のスロット"),
                           ("state", "状態"), ("trial", "試行先"), ("failure", "直近の失敗")):
            row = Adw.ActionRow(title=title, subtitle="—", subtitle_selectable=True)
            row.add_css_class("property")
            self.rows[key] = row
            g.add(row)
        self.show_btn = Gtk.Button(label="状態を表示", valign=Gtk.Align.CENTER)
        self.show_btn.connect("clicked", lambda _b: self.refresh())
        g.set_header_suffix(self.show_btn)
        page.add(g)

        a = Adw.PreferencesGroup(
            title="更新バンドルを適用",
            description="署名付きの更新バンドル（.qosb）を選ぶと、検証してから使っていない側のスロットに書き込みます。"
                        "書き込み後に再起動すると新しい版を試行起動し、健全性確認に失敗すると自動で元の版に戻ります。")
        self.bundle_row = Adw.ActionRow(title="更新バンドル", subtitle="未選択")
        choose = Gtk.Button(label="選択…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self.on_choose)
        self.bundle_row.add_suffix(choose)
        a.add(self.bundle_row)
        self.apply_btn = Gtk.Button(label="適用", css_classes=["suggested-action"], sensitive=False,
                                    halign=Gtk.Align.END, margin_top=12)
        self.apply_btn.connect("clicked", self.on_apply)
        self.progress = Gtk.ProgressBar(visible=False, margin_top=12, show_text=True)
        a.add(self.apply_btn)
        a.add(self.progress)
        page.add(a)

        r = Adw.PreferencesGroup(title="再起動")
        self.reboot_row = Adw.ActionRow(title="再起動して新しい版を試す",
                                        subtitle="更新は自動では再起動しません。作業を保存してから再起動してください。")
        self.reboot_btn = Gtk.Button(label="再起動…", valign=Gtk.Align.CENTER, sensitive=False,
                                     css_classes=["destructive-action"])
        self.reboot_btn.connect("clicked", self.on_reboot)
        self.reboot_row.add_suffix(self.reboot_btn)
        r.add(self.reboot_row)
        page.add(r)
        self.bundle: str | None = None

    # --- helpers -------------------------------------------------------------
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=5))

    def _set_busy(self, busy, text=None):
        self.busy = busy
        for w in (self.show_btn, self.refresh_btn):
            w.set_sensitive(not busy)
        self._update_apply()
        self.progress.set_visible(busy and text is not None)
        if busy and text:
            self.progress.set_text(text)
            self._pulse = GLib.timeout_add(150, lambda: (self.progress.pulse(), self.busy)[1])

    def _run_helper(self, argv, kind, done):
        def work():
            value = um.run(argv, kind)
            GLib.idle_add(lambda: (done(value), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _update_apply(self):
        ok = (not self.busy and self.bundle is not None
              and (self.status is None or self.status.can_apply))
        self.apply_btn.set_sensitive(ok)

    # --- status ----------------------------------------------------------------
    def refresh(self):
        self._set_busy(True)
        self._run_helper(um.status_argv(), "status", self._status_done)

    def _status_done(self, v):
        self._set_busy(False)
        if v is None:
            self.toast("認証が取り消されました")
            return
        if isinstance(v, um.AdminFailure):
            self.headline.set("dialog-error-symbolic", "更新状態を取得できません",
                              f"{v.text}\n{v.message}".strip())
            return
        self.show_status(v)

    def show_status(self, st: um.UpdateStatus):
        self.status = st
        self.rows["version"].set_subtitle(f"{st.confirmed_version or '不明'}（スロット {st.confirmed_slot or '?'}）")
        self.rows["booted"].set_subtitle(st.booted_slot or "不明")
        self.rows["state"].set_subtitle(st.state_text)
        self.rows["trial"].set_subtitle(
            f"スロット {st.trial_slot}（{st.trial_version}）" if st.trial_slot else "なし")
        self.rows["failure"].set_subtitle(um.describe_failure(st.last_failure))
        if st.state == "rolled_back":
            icon, title = "dialog-warning-symbolic", "前回の更新は適用されませんでした"
        elif st.needs_reboot:
            icon, title = "system-reboot-symbolic", "再起動すると更新を試行します"
        elif st.state == "trial_in_progress":
            icon, title = "content-loading-symbolic", "新しい版を試行起動中です"
        else:
            icon, title = "org.quantumos.UpdateManager", f"量子OS {st.confirmed_version or ''} で動作中"
        self.headline.set(icon, title, st.state_text)
        self.reboot_btn.set_sensitive(st.needs_reboot)
        self.head_reboot_btn.set_visible(st.needs_reboot)
        self._update_apply()

    # --- apply -----------------------------------------------------------------
    def on_choose(self, _b):
        f = Gtk.FileFilter(name="量子OS 更新バンドル")
        f.add_pattern("*.qosb")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        Gtk.FileDialog(title="更新バンドルを選択", filters=store).open(self, None, self._chosen)

    def _chosen(self, dlg, res):
        try:
            self.bundle = dlg.open_finish(res).get_path()
        except GLib.Error:
            return
        self.bundle_row.set_subtitle(GLib.markup_escape_text(self.bundle))
        self._update_apply()

    def on_apply(self, _b):
        dlg = Adw.AlertDialog(
            heading="更新を適用しますか？",
            body="署名と内容を検証してから、使っていない側のスロットに書き込みます。"
                 "管理者の認証が必要です。書き込み中は電源を切らないでください。")
        dlg.add_response("cancel", "キャンセル")
        dlg.add_response("apply", "適用")
        dlg.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda _d, r: r == "apply" and self._apply())
        dlg.present(self)

    def _apply(self):
        self._set_busy(True, "検証して書き込んでいます…")
        self._run_helper(um.apply_argv(self.bundle), "apply", self._apply_done)

    def _apply_done(self, v):
        self._set_busy(False)
        if v is None:
            self.toast("認証が取り消されました")
            return
        if isinstance(v, um.AdminFailure):
            dlg = Adw.AlertDialog(heading="更新を適用できません",
                                  body=f"{v.text}\n\n{v.message}")
            dlg.add_response("ok", "閉じる")
            dlg.present(self)
            return
        self.show_status(v)
        self.toast("書き込みが完了しました。再起動すると新しい版を試行します")

    def on_reboot(self, _b):
        dlg = Adw.AlertDialog(heading="今すぐ再起動しますか？",
                              body="保存していない作業は失われます。")
        dlg.add_response("cancel", "キャンセル")
        dlg.add_response("reboot", "再起動")
        dlg.set_response_appearance("reboot", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: r == "reboot" and self._reboot())
        dlg.present(self)

    def _reboot(self):
        # logind lets the active local user reboot; it asks for authentication otherwise
        try:
            subprocess.Popen(["systemctl", "reboot"])
        except OSError as exc:
            self.toast(f"再起動できません: {exc}")


class UpdateApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.get_active_window()
        if win is None:
            win = UpdateWindow(self)
            win.present()
            win.refresh()
        win.present()


def main(argv=None) -> int:
    return UpdateApp().run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
