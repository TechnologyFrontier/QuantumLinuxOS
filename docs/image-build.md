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

## 別の配布元で試す場合

パッケージ一覧と debootstrap の引数は上書きできます。たとえば Ubuntu のミラーでスクリプトの動作だけを確かめる場合：

```bash
sed 's/^linux-image-amd64$/linux-image-generic/' image/packages.txt > /tmp/packages-ubuntu.txt
```

```bash
sudo QOS_PACKAGES_FILE=/tmp/packages-ubuntu.txt DEBIAN_SUITE=noble DEBIAN_MIRROR=http://archive.ubuntu.com/ubuntu DEBOOTSTRAP_OPTS=--components=main,universe QOS_UPDATE_PUBKEY=update.pub image/build-rootfs.sh out-test
```

対象の基盤 OS は Debian 系の固定ベースです（QOS-BD 1章）。ほかの配布元はスクリプトの確認用です。
