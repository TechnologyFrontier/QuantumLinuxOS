"""Development environment setup (QOS-DT 3.3, T-DT-05)."""

import io
import json
import os
import tarfile
import zipfile

import pytest

from quantumos.desktop import devenv

PRODUCT = {
    "name": "IntelliJ IDEA", "version": "2025.3.6.2", "buildNumber": "253.33813.99",
    "productCode": "IU", "dataDirectoryName": "IntelliJIdea2025.3", "svgIconPath": "bin/idea.svg",
    "productVendor": "JetBrains",
    "launch": [{"os": "Linux", "arch": "amd64", "launcherPath": "bin/idea",
                "startupWmClass": "jetbrains-idea"}],
}


def _add(tf, name, data=b"", mode=0o644, kind=tarfile.REGTYPE, linkname=""):
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.type, ti.linkname = len(data), mode, kind, linkname
    tf.addfile(ti, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


def make_ide(path, product=PRODUCT, top="idea-IU-253.33813.99", extra=()):
    with tarfile.open(path, "w:gz") as tf:
        _add(tf, f"{top}/product-info.json", json.dumps(product).encode())
        _add(tf, f"{top}/bin/idea", b"#!/bin/sh\necho idea\n", 0o755)
        _add(tf, f"{top}/bin/idea.svg", b"<svg/>")
        for e in extra:
            _add(tf, **e)
    return str(path)


def make_plugin(path, since="253", until="253.*"):
    jar = io.BytesIO()
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("META-INF/plugin.xml",
                   f'<idea-plugin require-restart="true"><id>com.technologyfrontier.quantum-os-ide</id>'
                   f'<name>Quantum OS IDE</name><version>0.1.0</version>'
                   f'<idea-version since-build="{since}" until-build="{until}"/></idea-plugin>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("quantum-os-ide-plugin/lib/core.jar", b"core")
        z.writestr("quantum-os-ide-plugin/lib/quantum-os-ide-plugin-0.1.0.jar", jar.getvalue())
    return str(path)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return devenv.Paths(tmp_path / "home")


def test_inspect_archive_and_plugin(tmp_path):
    p = devenv.inspect_ide_archive(make_ide(tmp_path / "ide.tar.gz"))
    assert (p.version, p.build, p.data_directory, p.launcher) == (
        "2025.3.6.2", "253.33813.99", "IntelliJIdea2025.3", "bin/idea")
    plug = devenv.inspect_plugin_zip(make_plugin(tmp_path / "p.zip"))
    assert plug.plugin_id == "com.technologyfrontier.quantum-os-ide"
    assert plug.compatible_with("253.33813.99")
    assert plug.compatible_with("IU-253.1")
    assert not plug.compatible_with("252.9999")
    assert not plug.compatible_with("254.1")


def test_not_an_intellij_archive(tmp_path):
    with pytest.raises(devenv.SetupError, match="product-info.json"):
        devenv.inspect_ide_archive(make_ide(tmp_path / "a.tar.gz", top="x/y"))
    other = dict(PRODUCT, name="PyCharm")
    with pytest.raises(devenv.SetupError, match="IntelliJ IDEA ではありません"):
        devenv.inspect_ide_archive(make_ide(tmp_path / "b.tar.gz", product=other))
    (tmp_path / "c.tar.gz").write_bytes(b"not a tar")
    with pytest.raises(devenv.SetupError):
        devenv.inspect_ide_archive(str(tmp_path / "c.tar.gz"))
    bad = dict(PRODUCT, dataDirectoryName="../../evil")
    with pytest.raises(devenv.SetupError, match="不正"):
        devenv.inspect_ide_archive(make_ide(tmp_path / "d.tar.gz", product=bad))


def test_incompatible_plugin_is_refused(tmp_path, paths):
    ide = make_ide(tmp_path / "ide.tar.gz")
    plug = make_plugin(tmp_path / "p.zip", since="261", until="261.*")
    with pytest.raises(devenv.SetupError, match="対応していません"):
        devenv.plan(ide, plug, paths)


def test_install_into_user_area(tmp_path, paths):
    ide, plug = make_ide(tmp_path / "ide.tar.gz"), make_plugin(tmp_path / "p.zip")
    plan = devenv.plan(ide, plug, paths)
    assert not plan.replaces_ide and plan.warnings == ()
    msgs = []
    devenv.install(ide, plug, plan, msgs.append)
    home = paths.home
    launcher = home / ".local/opt/idea-2025.3.6.2/bin/idea"
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert (home / ".local/share/JetBrains/IntelliJIdea2025.3/quantum-os-ide-plugin/lib/core.jar").is_file()
    entry = (home / ".local/share/applications/quantumos-ide-2025.3.6.2.desktop").read_text()
    assert f'Exec="{launcher}" %f' in entry and "StartupWMClass=jetbrains-idea" in entry
    assert devenv.installed(paths) == [home / ".local/share/applications/quantumos-ide-2025.3.6.2.desktop"]
    # no leftovers from staging
    assert sorted(os.listdir(home / ".local/opt")) == ["idea-2025.3.6.2"]
    assert msgs[-1] == "完了しました"
    # reinstall replaces the same version
    plan2 = devenv.plan(ide, plug, paths)
    assert plan2.replaces_ide
    devenv.install(ide, plug, plan2)
    assert launcher.is_file()


def test_missing_plugin_installs_ide_only(tmp_path, paths):
    ide = make_ide(tmp_path / "ide.tar.gz")
    plan = devenv.plan(ide, str(tmp_path / "none.zip"), paths)
    assert plan.plugin is None and plan.warnings
    devenv.install(ide, None, plan)
    assert not (paths.home / ".local/share/JetBrains").exists()


def test_unsafe_archive_members_are_rejected(tmp_path, paths):
    ide = make_ide(tmp_path / "ide.tar.gz", extra=[
        {"name": "idea-IU-253.33813.99/lib/evil", "kind": tarfile.SYMTYPE, "linkname": "/etc/passwd"}])
    plan = devenv.plan(ide, None, paths)
    with pytest.raises(tarfile.TarError):
        devenv.install(ide, None, plan)
    assert not plan.ide_dir.exists()
    assert os.listdir(paths.home / ".local/opt") == []


def test_xdg_data_home_is_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    p = devenv.Paths(tmp_path / "home")
    info = devenv.inspect_ide_archive(make_ide(tmp_path / "ide.tar.gz"))
    assert p.plugins_dir(info) == tmp_path / "data/JetBrains/IntelliJIdea2025.3"
