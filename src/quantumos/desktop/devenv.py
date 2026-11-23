"""Development environment setup (QOS-DT 3.3).

Installs an IntelliJ IDEA distribution that the user obtained themselves
(``.tar.gz`` from JetBrains) into the user's own area and adds the QuantumOS
IDE plugin that ships with the desktop edition.  Nothing here needs
administrator rights, and the IDE is never redistributed by the OS: the user
accepts JetBrains' terms when the IDE starts for the first time.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

BUNDLED_PLUGIN_ZIP = "/usr/share/quantumos/ide/quantum-os-ide-plugin.zip"
DESKTOP_FILE_PREFIX = "quantumos-ide-"
MAX_PRODUCT_INFO_BYTES = 4 * 1024 * 1024


class SetupError(Exception):
    pass


@dataclass(frozen=True)
class ProductInfo:
    name: str
    version: str
    build: str
    product_code: str
    data_directory: str
    launcher: str  # relative to the IDE home
    icon: str | None
    wm_class: str | None
    top_dir: str  # top-level directory inside the archive

    @property
    def install_dir_name(self) -> str:
        return f"idea-{self.version}"


@dataclass(frozen=True)
class PluginInfo:
    plugin_id: str
    name: str
    version: str
    since_build: str | None
    until_build: str | None
    root_dir: str  # top-level directory inside the zip

    def compatible_with(self, build: str) -> bool:
        b = _build_tuple(build)
        if self.since_build and b < _build_tuple(self.since_build):
            return False
        if self.until_build:
            parts = self.until_build.split(".")
            if parts[-1] == "*":
                prefix = _build_tuple(".".join(parts[:-1]))
                return b[: len(prefix)] <= prefix
            return b <= _build_tuple(self.until_build)
        return True


def _build_tuple(build: str) -> tuple[int, ...]:
    # "IU-253.33813.99" → (253, 33813, 99)
    build = build.split("-", 1)[-1]
    out = []
    for p in build.split("."):
        if not p.isdigit():
            break
        out.append(int(p))
    if not out:
        raise SetupError(f"ビルド番号を解釈できません: {build}")
    return tuple(out)


_SAFE_NAME = re.compile(r"[A-Za-z0-9._+-]+\Z")


def inspect_ide_archive(path: str) -> ProductInfo:
    """Read product-info.json from an IntelliJ IDEA Linux .tar.gz."""
    try:
        tf = tarfile.open(path, "r:*")
    except (OSError, tarfile.TarError) as exc:
        raise SetupError(f"配布物を開けません: {exc}") from exc
    with tf:
        info = None
        for member in tf:
            parts = member.name.split("/")
            if len(parts) == 2 and parts[1] == "product-info.json" and member.isfile():
                if member.size > MAX_PRODUCT_INFO_BYTES:
                    raise SetupError("product-info.json が大きすぎます")
                fh = tf.extractfile(member)
                info = (parts[0], json.load(io.TextIOWrapper(fh, "utf-8")))
                break
    if info is None:
        raise SetupError("IntelliJ IDEA の配布物ではありません（product-info.json がありません）")
    top, d = info
    if d.get("productVendor") != "JetBrains" or not str(d.get("name", "")).startswith("IntelliJ IDEA"):
        raise SetupError(f"IntelliJ IDEA ではありません: {d.get('name')}")
    linux = [x for x in d.get("launch", []) if x.get("os") == "Linux"]
    if not linux:
        raise SetupError("Linux 用の配布物ではありません")
    launch = next((x for x in linux if x.get("arch") in ("amd64", "x86_64")), linux[0])
    version, data_dir = str(d.get("version", "")), str(d.get("dataDirectoryName", ""))
    for value in (version, data_dir, top):
        if not _SAFE_NAME.match(value):
            raise SetupError(f"配布物の情報が不正です: {value!r}")
    return ProductInfo(
        name=d["name"],
        version=version,
        build=str(d.get("buildNumber", "")),
        product_code=str(d.get("productCode", "")),
        data_directory=data_dir,
        launcher=str(launch.get("launcherPath", "bin/idea")),
        icon=d.get("svgIconPath"),
        wm_class=launch.get("startupWmClass"),
        top_dir=top,
    )


def inspect_plugin_zip(path: str) -> PluginInfo:
    """Read META-INF/plugin.xml from the jar inside an IDE plugin zip."""
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise SetupError(f"プラグインを開けません: {exc}") from exc
    with zf:
        names = zf.namelist()
        roots = {n.split("/", 1)[0] for n in names}
        if len(roots) != 1:
            raise SetupError("プラグイン ZIP の構成が不正です")
        root = roots.pop()
        for n in names:
            if not (n.startswith(f"{root}/lib/") and n.endswith(".jar")):
                continue
            try:
                jar = zipfile.ZipFile(io.BytesIO(zf.read(n)))
            except zipfile.BadZipFile:
                continue
            with jar:
                if "META-INF/plugin.xml" in jar.namelist():
                    try:
                        xml = ET.fromstring(jar.read("META-INF/plugin.xml"))
                    except ET.ParseError as exc:
                        raise SetupError(f"plugin.xml を解釈できません: {exc}") from exc
                    iv = xml.find("idea-version")
                    return PluginInfo(
                        plugin_id=(xml.findtext("id") or "").strip(),
                        name=(xml.findtext("name") or "").strip(),
                        version=(xml.findtext("version") or "").strip(),
                        since_build=iv.get("since-build") if iv is not None else None,
                        until_build=iv.get("until-build") if iv is not None else None,
                        root_dir=root,
                    )
    raise SetupError("プラグインの記述（plugin.xml）が見つかりません")


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def for_user(cls) -> "Paths":
        return cls(Path(os.path.expanduser("~")))

    @property
    def data_home(self) -> Path:
        x = os.environ.get("XDG_DATA_HOME")
        return Path(x) if x and os.path.isabs(x) else self.home / ".local/share"

    @property
    def opt_dir(self) -> Path:
        return self.home / ".local/opt"

    def ide_dir(self, p: ProductInfo) -> Path:
        return self.opt_dir / p.install_dir_name

    def plugins_dir(self, p: ProductInfo) -> Path:
        return self.data_home / "JetBrains" / p.data_directory

    def desktop_file(self, p: ProductInfo) -> Path:
        return self.data_home / "applications" / f"{DESKTOP_FILE_PREFIX}{p.version}.desktop"


@dataclass(frozen=True)
class Plan:
    product: ProductInfo
    plugin: PluginInfo | None
    ide_dir: Path
    plugins_dir: Path
    desktop_file: Path
    replaces_ide: bool
    warnings: tuple[str, ...]


def plan(archive: str, plugin_zip: str | None, paths: Paths) -> Plan:
    product = inspect_ide_archive(archive)
    plugin = None
    warnings = []
    if plugin_zip:
        if os.path.exists(plugin_zip):
            plugin = inspect_plugin_zip(plugin_zip)
            if not plugin.compatible_with(product.build):
                raise SetupError(
                    f"{plugin.name} {plugin.version} はこの IDE（ビルド {product.build}）に対応していません"
                    f"（対応範囲 {plugin.since_build}〜{plugin.until_build}）")
        else:
            warnings.append("量子OSプラグインが見つかりません。IDE のみ導入します。")
    ide_dir = paths.ide_dir(product)
    return Plan(product, plugin, ide_dir, paths.plugins_dir(product), paths.desktop_file(product),
                ide_dir.exists(), tuple(warnings))


def _desktop_entry(p: Plan) -> str:
    launcher = p.ide_dir / p.product.launcher
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={p.product.name} {p.product.version}",
        "Comment=量子OSプラグイン入りの IntelliJ IDEA",
        f"Exec=\"{launcher}\" %f",
        "Terminal=false",
        "Categories=Development;IDE;",
        "StartupNotify=true",
    ]
    if p.product.icon:
        lines.append(f"Icon={p.ide_dir / p.product.icon}")
    if p.product.wm_class:
        lines.append(f"StartupWMClass={p.product.wm_class}")
    return "\n".join(lines) + "\n"


def install(archive: str, plugin_zip: str | None, plan_: Plan, progress=lambda msg: None) -> None:
    """Carry out a plan.  Replaces an earlier copy of the same IDE version."""
    plan_.ide_dir.parent.mkdir(parents=True, exist_ok=True)
    progress("IDE を展開しています…")
    work = Path(tempfile.mkdtemp(prefix=".qos-ide-", dir=plan_.ide_dir.parent))
    try:
        with tarfile.open(archive, "r:*") as tf:
            tf.extractall(work, filter="data")
        src = work / plan_.product.top_dir
        if not (src / plan_.product.launcher).is_file():
            raise SetupError("展開した IDE に起動コマンドがありません")
        if plan_.ide_dir.exists():
            shutil.rmtree(plan_.ide_dir)
        src.rename(plan_.ide_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if plan_.plugin and plugin_zip:
        progress("量子OSプラグインを導入しています…")
        plan_.plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plan_.plugins_dir / plan_.plugin.root_dir
        stage = Path(tempfile.mkdtemp(prefix=".qos-plugin-", dir=plan_.plugins_dir))
        try:
            with zipfile.ZipFile(plugin_zip) as zf:
                for n in zf.namelist():
                    dest = (stage / n).resolve()
                    if not str(dest).startswith(str(stage.resolve()) + os.sep):
                        raise SetupError(f"プラグイン ZIP に不正なパスがあります: {n}")
                zf.extractall(stage)
            if target.exists():
                shutil.rmtree(target)
            (stage / plan_.plugin.root_dir).rename(target)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    progress("起動項目を作成しています…")
    plan_.desktop_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = plan_.desktop_file.with_suffix(".tmp")
    tmp.write_text(_desktop_entry(plan_), encoding="utf-8")
    tmp.replace(plan_.desktop_file)
    progress("完了しました")


def installed(paths: Paths) -> list[Path]:
    """Desktop entries created by this tool."""
    apps = paths.data_home / "applications"
    return sorted(apps.glob(f"{DESKTOP_FILE_PREFIX}*.desktop")) if apps.is_dir() else []
