# QuantumLinuxOS

量子回路のローカル実行を標準搭載した Linux ベース OS（設計上の呼称「量子OS」）の実装リポジトリです。
設計書 QOS-BD／QOS-FD／QOS-IF（各 0.1、レビュー前・未承認）に基づく初版実装です。

> **状態**: 設計レビュー用の初版実装です。量子サービス・CLI・更新ツールは単体／結合テストで検証済みです。
> OS イメージの作成と実機・仮想機での起動評価は**まだ行っていません**（QOS-BD 4.2）。

## 構成

| 層 | 実装 | 機能ID |
|---|---|---|
| UEFI・ブートローダー | systemd-boot。スロットごとにエントリーを1つ置き、試行起動は one-shot で行う | OS-01／06 |
| Linux・システム管理 | Debian 系の固定ベース、読み取り専用ルート A/B、状態領域 `/var`、`/etc` オーバーレイ（[image/](image/)） | OS-01／02 |
| 量子サービス | `qosd`：Unix ソケット・版付き JSON・状態ベクトル計算（[src/quantumos/service.py](src/quantumos/service.py)） | OS-03〜05 |
| 標準 CLI | `qosctl validate / run / status`（[src/quantumos/cli.py](src/quantumos/cli.py)） | OS-03〜05 |
| 保守・診断 | `qos-admin update / update-status / boot-assess`（[src/quantumos/admin/](src/quantumos/admin/)） | OS-06／07 |
| デスクトップ版 | GNOME、量子回路スタジオ・更新マネージャー・開発環境セットアップ（[src/quantumos/desktop/](src/quantumos/desktop/)、[image/editions/desktop/](image/editions/desktop/)） | DT-01〜05 |

```
src/quantumos/        量子サービス・CLI・IPC・回路検証・シミュレーター
src/quantumos/admin/  更新バンドル検証・A/B 書き込み・試行起動判定・切り戻し
src/quantumos/desktop/ デスクトップ版のアプリ（GTK4/libadwaita。画面以外のロジックは pytest で試験）
system/               systemd ユニット、sysusers、tmpfiles、設定ファイル、デスクトップ版の起動項目・polkit
image/                ルートファイルシステム／ディスクイメージ／更新バンドルの作成スクリプト（editions/ に版ごとの差分）
tests/                受入試験 T-OS-02〜06 と IPC 適合確認の自動テスト
docs/design/          設計書 0.1 の Markdown 版
docs/                 実装上の解釈・未確定事項・トレーサビリティ
examples/             回路 JSON の例
```

## 使い方（OS 上）

量子サービスは `qosd.socket` によるソケット起動で、`/run/quantumos/quantum.sock`（root:quantum、0660）で待ち受けます。
`quantum` グループの利用者が CLI を使えます。

```bash
sudo useradd -m -G quantum alice
```

```bash
qosctl validate circuit.json
```

```bash
qosctl run circuit.json --shots 8 --seed 0
```

```bash
qosctl status
```

成功時は終了値 0 で、結果の JSON を標準出力に 1 行出力します。失敗時は `{"code":…,"message":…}` を標準エラー出力に 1 行出力します。

| 終了値 | 区分 |
|---|---|
| 0 | 成功 |
| 2 | CLI 引数、回路、形式、版、実行オプション、実行上限の違反 |
| 3 | 接続不能、権限拒否、計算枠なし（BUSY） |
| 4 | 計算失敗、期限超過、途中切断または応答不正 |

管理者による更新（自動では再起動しません）：

```bash
sudo qos-admin update quantumos-0.2.0.qosb
```

```bash
sudo qos-admin update-status
```

## 開発環境での実行

Python 3.11 以上、NumPy、cryptography が必要です。

```bash
pip install -e '.[test]'
```

```bash
python3 -m pytest
```

インストールせずにソースツリーからサービスと CLI を動かす場合：

```bash
echo '{"allowed_group": null, "allowed_uids": ['"$(id -u)"'], "socket_group": null}' > /tmp/qosd.json
```

```bash
./bin/qosd --config /tmp/qosd.json --socket /tmp/quantum.sock &
```

```bash
QOS_SOCKET=/tmp/quantum.sock ./bin/qosctl run examples/x1-measure10.json --shots 8 --seed 0
```

```
{"counts":[["10",8]],"shots":8,"seed":"0","measured_qubits":[1,0],"backend_id":"local_statevector","service_version":"0.1.0"}
```

## OS イメージの作成

手順の詳細は [docs/image-build.md](docs/image-build.md) を参照してください。root 権限、debootstrap、Debian ミラーへの接続が必要です。

```bash
./bin/qos-bundle keygen update.key update.pub
```

```bash
sudo QOS_UPDATE_PUBKEY=update.pub QOS_ROOT_PASSWORD_HASH="$(openssl passwd -6)" image/build-rootfs.sh out
```

```bash
sudo image/make-disk.sh out quantumos.raw
```

GNOME 付きのデスクトップ版は `QOS_EDITION=desktop` を付けて同じスクリプトで作ります（[docs/image-build.md](docs/image-build.md#デスクトップ版を作るqos-dt-01)）。

## デスクトップ版

量子OSには CLI のみの標準版と、GNOME 48 を載せたデスクトップ版があります（[設計書 QOS-DT 0.1](docs/design/QOS-DT-0.1.md)）。
デスクトップ版は汎用デスクトップとしての機能（ブラウザー、ファイル、日本語入力、ネットワーク設定、GNOME ソフトウェアによる Flatpak アプリの追加）に加えて、次を備えます。

| アプリ | コマンド | 内容 |
|---|---|---|
| 量子回路スタジオ | `qos-studio` | 回路の編集（ゲートの追加・並べ替え・角度）、回路図、構造チェック、`qosctl` による検証・実行・停止、ヒストグラムと表、同じシードでの再実行、結果の明示保存 |
| 量子OS 更新マネージャー | `qos-update-manager` | A/B スロットと試行起動・切り戻しの状態表示、署名付き更新バンドルの適用（polkit の管理者認証）、再起動の案内 |
| 開発環境セットアップ | `qos-devenv-setup` | 利用者が入手した IntelliJ IDEA（.tar.gz）を `~/.local/opt` に展開し、OS 同梱の量子OSプラグインと起動項目を導入（管理者権限不要） |

読み取り専用ルートと A/B 更新（署名検証・試行起動・自動切り戻し）はデスクトップ全体に適用されます。

## ドキュメント

- [docs/implementation-notes.md](docs/implementation-notes.md) — 設計書の解釈、暫定で決めた事項、未確定事項、要件と実装・試験の対応表
- [docs/update-and-recovery.md](docs/update-and-recovery.md) — 更新・試行起動・切り戻しの流れと失敗コード
- [docs/image-build.md](docs/image-build.md) — イメージ作成とディスク構成
- [docs/design/](docs/design/) — 設計書 0.1（基本設計書・機能設計書・インターフェース仕様書）
