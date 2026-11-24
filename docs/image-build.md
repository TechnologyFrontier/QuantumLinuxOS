# OS イメージの作成

> 起動評価はまだ行っていません。手順とスクリプトは初版案です（QOS-BD 4.2）。

## 必要なもの

- x86_64 の Linux ホストと root 権限
- `debootstrap`、`e2fsprogs`（`mkfs.ext4 -d`）、`dosfstools`、`util-linux`（`sfdisk`、`losetup`）、`systemd-boot`（`bootctl`）
- Debian ミラーへの接続
- Python 3.11 以上と `cryptography`（更新バンドルの作成用）

ビルドの設定は [image/config.env](../image/config.env) にあります。どの値も環境変数で上書きできます。

## 1. 更新署名鍵を作る（配布元で1回）

```bash
./bin/qos-bundle keygen update.key update.pub
```

秘密鍵 `update.key` は配布元で管理し、イメージにも利用者にも渡しません。公開鍵はイメージの `/usr/share/quantumos/keys/update-ed25519.pub` に入ります。

## 2. ルートファイルシステムを作る

```bash
sudo QOS_UPDATE_PUBKEY=update.pub QOS_ROOT_PASSWORD_HASH="$(openssl passwd -6)" image/build-rootfs.sh out
```

`out/` には次のファイルができます。

| ファイル | 内容 |
|---|---|
| `root.img` | 読み取り専用ルートの ext4 イメージ（1スロット分） |
| `vmlinuz`、`initrd.img` | カーネルと initramfs（`quantumos-state` フックを含む） |
| `var-seed/` | 新しい状態領域の初期内容 |
| `rootfs/` | 確認用のルートツリー |

イメージに入るもの：

- `qosd`、`qosctl`、`qos-admin`（`/usr/bin`、Python パッケージは `/usr/lib/python3/dist-packages/quantumos`）
- `qosd.socket`、`qosd.service`、`qos-boot-assess.service`（有効化済み）
- `quantum` グループと、サービス用の非特権アカウント `qos-svc`（systemd-sysusers）
- 管理コンソール：`getty@tty1`、`serial-getty@ttyS0`

ネットワークサービスは入れません。`qosd` は `PrivateNetwork=yes` で動くため、IP の待受はできません。

## 3. ディスクイメージを作る

```bash
sudo image/make-disk.sh out quantumos.raw
```

2つ目の引数にブロックデバイスを指定すると、そのディスクに直接書き込みます。**指定したディスクの内容はすべて消えます。**

| # | PARTLABEL | 既定サイズ | 内容 |
|---|---|---|---|
| 1 | `ESP` | 1 GiB | systemd-boot、スロットごとのカーネル |
| 2 | `qos-root-a` | 6 GiB | ルート A（初期の確定済みスロット） |
| 3 | `qos-root-b` | 6 GiB | ルート B（最初の更新までは空） |
| 4 | `qos-state` | 残り | `/var` |

## 4. 仮想マシンで起動する（評価用）

Secure Boot を無効にした OVMF で起動します。

```bash
qemu-system-x86_64 -machine q35,accel=kvm -smp 2 -m 4096 -bios /usr/share/ovmf/OVMF.fd -drive file=quantumos.raw,format=raw,if=virtio -nographic
```

## 5. 更新バンドルを作る

版を上げてルートを作り直し、署名付きバンドルにします。

```bash
sudo QOS_VERSION=0.2.0 QOS_UPDATE_PUBKEY=update.pub QOS_ROOT_PASSWORD_HASH="$(openssl passwd -6)" image/build-rootfs.sh out-0.2.0
```

```bash
image/make-bundle.sh out-0.2.0 update.key 0.2.0 quantumos-0.2.0.qosb
```

```bash
./bin/qos-bundle verify --pubkey update.pub quantumos-0.2.0.qosb
```

OS 上での適用方法は [update-and-recovery.md](update-and-recovery.md) を参照してください。

## デスクトップ版を作る（QOS-DT 0.1）

`QOS_EDITION=desktop` を付けると、標準版のパッケージに GNOME と量子OSのデスクトップアプリを加えたデスクトップ版を作ります。
標準版の手順と出力は変わりません。

```bash
sudo QOS_EDITION=desktop QOS_UPDATE_PUBKEY=update.pub QOS_ROOT_PASSWORD_HASH="$(openssl passwd -6)" QOS_IDE_PLUGIN_ZIP=quantum-os-ide-plugin-0.1.0.zip image/build-rootfs.sh out-desktop
```

```bash
sudo QOS_EDITION=desktop image/make-disk.sh out-desktop quantumos-desktop.raw
```

| 項目 | 標準版 | デスクトップ版 |
|---|---|---|
| 追加パッケージ | — | [image/editions/desktop/packages.txt](../image/editions/desktop/packages.txt)（apt で導入。Recommends なし） |
| 製品 ID | `quantumos` | `quantumos-desktop`（更新バンドルも `QOS_EDITION=desktop image/make-bundle.sh …` で作る） |
| ルートスロット／ルートイメージ／ディスク | 6／3／32 GiB | 12／6／64 GiB |
| 既定ターゲット | multi-user（`getty@tty1`） | graphical（GDM） |
| 健全性確認 | [system/config/update.conf](../system/config/update.conf) の既定 | [image/editions/desktop/update.conf](../image/editions/desktop/update.conf)（tty1 の代わりに `display-manager.service`） |

デスクトップ版にだけ入るもの（[image/editions/desktop/configure.sh](../image/editions/desktop/configure.sh)）：

- `qos-studio`（量子回路スタジオ）、`qos-update-manager`（更新マネージャー）、`qos-devenv-setup`（開発環境セットアップ）と起動項目・アイコン
- `/usr/libexec/quantumos/qos-update-helper` と polkit の操作 `org.quantumos.update.status`／`org.quantumos.update.apply`
- `QOS_IDE_PLUGIN_ZIP` を指定した場合は量子OSプラグインを `/usr/share/quantumos/ide/quantum-os-ide-plugin.zip` に同梱（IntelliJ IDEA 本体は同梱しない）
- `/etc/adduser.conf` の `EXTRA_GROUPS="users quantum"`（GNOME で作った利用者が量子サービスを使える）
- ロケール `ja_JP.UTF-8`（既定）と `en_US.UTF-8`、Mozc、Noto CJK フォント
- PackageKit の無効化（GNOME ソフトウェアは Flatpak のアプリだけを扱う。Flatpak は `/var/lib/flatpak` に入り、A/B 更新・切り戻しの影響を受けない）

Flatpak のリモートは登録していません。Flathub を使う場合は管理者が次を実行します。

```bash
sudo flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
```

## 別の配布元で試す場合

パッケージ一覧と debootstrap の引数は上書きできます。たとえば Ubuntu のミラーでスクリプトの動作だけを確かめる場合：

```bash
sed 's/^linux-image-amd64$/linux-image-generic/' image/packages.txt > /tmp/packages-ubuntu.txt
```

```bash
sudo QOS_PACKAGES_FILE=/tmp/packages-ubuntu.txt DEBIAN_SUITE=noble DEBIAN_MIRROR=http://archive.ubuntu.com/ubuntu DEBOOTSTRAP_OPTS=--components=main,universe QOS_UPDATE_PUBKEY=update.pub image/build-rootfs.sh out-test
```

対象の基盤 OS は Debian 系の固定ベースです（QOS-BD 1章）。ほかの配布元はスクリプトの確認用です。
