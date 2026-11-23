"""開発環境セットアップ – GTK4/libadwaita front end (QOS-DT DT-04)."""

from __future__ import annotations

import os
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import devenv  # noqa: E402

APP_ID = "org.quantumos.DevSetup"
DOWNLOAD_HINT = "https://www.jetbrains.com/idea/download/"


class DevSetupWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="開発環境セットアップ")
        self.set_default_size(720, 620)
        self.paths = devenv.Paths.for_user()
        self.plugin_zip = os.environ.get("QOS_IDE_PLUGIN_ZIP", devenv.BUNDLED_PLUGIN_ZIP)
        self.archive: str | None = None
        self.plan: devenv.Plan | None = None

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        page = Adw.PreferencesPage()
        view.set_content(page)

        intro = Adw.PreferencesGroup(
            title="量子回路の開発環境",
            description="IntelliJ IDEA と量子OSプラグインを、あなたのホーム（~/.local）に導入します。"
                        "管理者権限は使いません。IntelliJ IDEA は JetBrains の配布物（Linux 用 .tar.gz）を"
                        "ご自身で入手してください。利用規約への同意は IDE の初回起動時に行います。")
        link = Gtk.LinkButton(uri=DOWNLOAD_HINT, label="入手先（JetBrains）", halign=Gtk.Align.START)
        intro.add(link)
        page.add(intro)

        g = Adw.PreferencesGroup(title="導入するもの")
        self.archive_row = Adw.ActionRow(title="IntelliJ IDEA の配布物", subtitle="未選択")
        choose = Gtk.Button(label="選択…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self.on_choose)
        self.archive_row.add_suffix(choose)
        g.add(self.archive_row)
        self.plugin_row = Adw.ActionRow(title="量子OSプラグイン（OS 同梱）")
        g.add(self.plugin_row)
        self.dest_row = Adw.ActionRow(title="導入先", subtitle=str(self.paths.opt_dir))
        g.add(self.dest_row)
        page.add(g)

        self._describe_plugin()

        act = Adw.PreferencesGroup()
        self.install_btn = Gtk.Button(label="導入", css_classes=["suggested-action", "pill"],
                                      sensitive=False, halign=Gtk.Align.CENTER)
        self.install_btn.connect("clicked", self.on_install)
        self.progress = Gtk.ProgressBar(show_text=True, visible=False, margin_top=12)
        act.add(self.install_btn)
        act.add(self.progress)
        page.add(act)

        self.installed_group = Adw.PreferencesGroup(title="導入済み")
        page.add(self.installed_group)
        self._installed_rows = []
        self.refresh_installed()

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=5))

    def _describe_plugin(self):
        try:
            p = devenv.inspect_plugin_zip(self.plugin_zip)
            self.plugin_row.set_subtitle(
                f"{p.name} {p.version}（対応ビルド {p.since_build}〜{p.until_build}）")
        except devenv.SetupError as exc:
            self.plugin_row.set_subtitle(GLib.markup_escape_text(f"利用できません: {exc}"))

    def refresh_installed(self):
        for row in self._installed_rows:
            self.installed_group.remove(row)
        self._installed_rows = []
        entries = devenv.installed(self.paths)
        if not entries:
            row = Adw.ActionRow(title="まだありません")
            self.installed_group.add(row)
            self._installed_rows.append(row)
        for entry in entries:
            info = Gio.DesktopAppInfo.new_from_filename(str(entry))
            row = Adw.ActionRow(title=info.get_display_name() if info else entry.stem,
                                subtitle=str(entry))
            if info:
                b = Gtk.Button(label="起動", valign=Gtk.Align.CENTER)
                b.connect("clicked", lambda _b, i=info: i.launch([], None))
                row.add_suffix(b)
            self.installed_group.add(row)
            self._installed_rows.append(row)

    def on_choose(self, _b):
        f = Gtk.FileFilter(name="IntelliJ IDEA（.tar.gz）")
        f.add_pattern("*.tar.gz")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        Gtk.FileDialog(title="IntelliJ IDEA の配布物を選択", filters=store).open(self, None, self._chosen)

    def _chosen(self, dlg, res):
        try:
            path = dlg.open_finish(res).get_path()
        except GLib.Error:
            return
        self.archive_row.set_subtitle("確認しています…")
        self.install_btn.set_sensitive(False)

        def work():
            try:
                return devenv.plan(path, self.plugin_zip, self.paths)
            except devenv.SetupError as exc:
                return exc

        def done(v):
            if isinstance(v, devenv.SetupError):
                self.archive, self.plan = None, None
                self.archive_row.set_subtitle(GLib.markup_escape_text(f"使えません: {v}"))
                return
            self.archive, self.plan = path, v
            text = f"{v.product.name} {v.product.version}（ビルド {v.product.build}）"
            if v.replaces_ide:
                text += "・導入済みの同じ版を置き換えます"
            for w in v.warnings:
                text += f"・{w}"
            self.archive_row.set_subtitle(GLib.markup_escape_text(text))
            self.dest_row.set_subtitle(str(v.ide_dir))
            self.install_btn.set_sensitive(True)
        threading.Thread(target=lambda: GLib.idle_add(lambda: (done(work()), False)[1]),
                         daemon=True).start()

    def on_install(self, _b):
        plan, archive = self.plan, self.archive
        if not plan:
            return
        self.install_btn.set_sensitive(False)
        self.progress.set_visible(True)
        pulse = GLib.timeout_add(150, lambda: (self.progress.pulse(), True)[1])

        def progress(msg):
            GLib.idle_add(lambda: (self.progress.set_text(msg), False)[1])

        def work():
            try:
                devenv.install(archive, self.plugin_zip if plan.plugin else None, plan, progress)
                return None
            except (devenv.SetupError, OSError, Exception) as exc:  # report every failure in the UI
                return exc

        def done(err):
            GLib.source_remove(pulse)
            self.progress.set_visible(False)
            self.install_btn.set_sensitive(True)
            if err:
                self.toast(f"導入できません: {err}")
                return
            self.toast("導入しました。アプリ一覧またはここから起動できます")
            self.refresh_installed()
        threading.Thread(target=lambda: GLib.idle_add(lambda: (done(work()), False)[1]),
                         daemon=True).start()


class DevSetupApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        (self.get_active_window() or DevSetupWindow(self)).present()


def main(argv=None) -> int:
    return DevSetupApp().run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
