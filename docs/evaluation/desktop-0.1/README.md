# デスクトップ版 0.1 の仮想マシン評価（QOS-DT 0.1）

実施日: 2026-11-24
対象: QuantumLinuxOS `feature/desktop-edition`（評価開始時 ea2c563、評価中の修正 868655a・127dbee を含む）

## 評価構成

| 項目 | 内容 |
|---|---|
| イメージ | `QOS_EDITION=desktop` で作成した 0.1.0（Debian 13 trixie、GNOME 48、ルート 3.0 GiB）。量子OSプラグイン 0.1.0 を同梱 |
| 更新バンドル | 修正を含む 0.2.0／0.2.1（`QOS_EDITION=desktop`、製品 ID `quantumos-desktop`）と、標準版の製品 ID の試験用バンドル（1 MiB のルートイメージ） |
| 仮想マシン | qemu q35、UEFI（OVMF、Secure Boot 無効）、2 vCPU、8 GiB、virtio ディスク 64 GiB、std VGA（bochs-drm、llvmpipe で描画）、USB タブレット・キーボード、**ネットワークなし**。ホストに仮想化支援がないため TCG |
| 操作 | 画面は QMP の screendump、入力は QMP の `input-send-event`／`send-key`（Wayland のため xdotool は使えない。[tools/qin.py](tools/qin.py)）。保守操作はシリアルコンソールの root シェル（[tools/vmctl.py](tools/vmctl.py)） |
| 利用者 | GNOME の初期設定で作成した `alicetester`（管理者） |

## 結果

| # | 確認項目 | 試験 | 結果 | 証跡 |
|---|---|---|---|---|
| 1 | UEFI で起動し、GNOME の初期設定（日本語、Mozc、プライバシー、タイムゾーン、利用者、パスワード）が表示される | T-DT-01 | 合格 | [01](01-initial-setup.png)〜[04](04-setup-done.png) |
| 2 | 初期設定で作成した利用者が `sudo`・`users`・`quantum` グループに属する | T-DT-07 | 合格（`groups=…,27(sudo),100(users),990(quantum)`） | [03](03-user.png) |
| 3 | `/` 読み取り専用、`/var` 状態領域、`/etc` オーバーレイ、`display-manager`・`qosd.socket`・NetworkManager 稼働、PackageKit 停止 | T-DT-01 | 合格 | — |
| 4 | GNOME の Dash に量子回路スタジオ・更新マネージャー（0.2.1 で端末も） | DT-01 | 合格 | [06](06-overview-dash.png)、[23](23-dash-021.png) |
| 5 | GDM のログイン画面から利用者名・パスワードでログイン | T-DT-01 | 合格 | [16](16-gdm-login.png) |
| 6 | 量子回路スタジオ：`/usr/bin/qosctl` 経由で状態（ready）、ベル回路 1,024 ショット（00=534、11=490） | T-DT-02 | 合格 | [07](07-studio.png)、[08](08-studio-bell.png) |
| 7 | 別プロセスの計算中に実行 → BUSY の案内、再試行なし、直前の結果を保持 | T-DT-03 | 合格（qosd の BUSY 記録 1 件） | [09](09-studio-busy.png) |
| 8 | 20 量子ビット・235 操作の回路をファイルから開き（回路図）、実行中に停止 → サービスは `CLIENT_DISCONNECTED` で計算枠を解放 | T-DT-03 | 合格 | [10](10-studio-long.png)、[11](11-studio-stopped.png) |
| 9 | 結果・シードの自動保存なし、一時回路ファイルの残りなし | T-DT-03 | 合格（`/var/home`、`/var/log`、`/var/lib/quantumos`、`/tmp`、`/run/user/1000` に該当なし） | — |
| 10 | 同じシードで再実行 → 同じ観測回数（00=527、11=497） | DT-02 | 合格（0.2.1） | [24](24-studio-021-run.png) |
| 11 | 更新マネージャー：状態表示は sudo グループの利用者ならパスワードなし | T-DT-04 | 合格 | [12](12-update-status.png) |
| 12 | 更新の適用は毎回管理者認証を求める（polkit `org.quantumos.update.apply`） | T-DT-04 | 合格 | [17](17-update-auth.png) |
| 13 | 標準版の製品 ID のバンドルを拒否 | T-DT-04 | 合格（PRODUCT_MISMATCH を日本語で説明） | [18](18-update-product-mismatch.png) |
| 14 | デスクトップ版バンドルの検証・書き込み → 試行予約、再起動の案内 | T-DT-04 | 合格（6.5 GB で約 7 分、9.7 GB で約 9 分） | [19](19-update-trial-reserved.png)、[22](22-update-021-reserved.png) |
| 15 | 試行起動で健全性確認に失敗 → 自動で元の版に戻り、更新マネージャーが理由を表示 | OS-06 | 合格（下記の不具合 1 による失敗で確認） | [20](20-update-rolled-back.png) |
| 16 | 修正版 0.2.1 の試行起動 → GDM を含む健全性確認 6 項目に合格して確定 | T-DT-06 | 合格（`slot b (0.2.1) confirmed`） | — |
| 17 | 更新・切り戻しの後も利用者、ホーム、導入した IDE とプラグインが残る | QOS-DT 4章 | 合格 | — |
| 18 | 開発環境セットアップ：配布物（2025.3.6.2、ビルド 253）とプラグインの互換確認、`~/.local` への導入（約 4 分）、起動項目 | T-DT-05 | 合格（導入物はすべて利用者の所有、root のプロセスなし） | [13](13-devsetup-plan.png)、[14](14-devsetup-done.png) |
| 19 | 起動項目から IDE が起動する | T-DT-05 | 合格（初回の「言語と地域」画面で終了。JetBrains の利用規約には同意していない） | [15](15-ide-first-run.png) |

## 評価で見つけて修正した不具合

| # | 不具合 | 修正 | 確認 |
|---|---|---|---|
| 1 | **試行起動の判定が GDM より先に走り、display-manager の確認が失敗して切り戻された。** `qos-boot-assess` は multi-user.target の一部で、GDM は graphical.target で起動するため | デスクトップ版で `qos-boot-assess.service` を `display-manager.service` の後に並べる drop-in を追加（127dbee） | 0.2.1 で確定（結果 16） |
| 2 | 結果の「同じシードで再実行」「結果を保存」ボタンが 1280×800 では画面外 | 結果の見出しの横へ移動（868655a） | [24](24-studio-021-run.png) |
| 3 | 新しい実行を始めても前回の BUSY 表示が残る。量子サービスの状態表示が古いまま | 実行開始時に消す。待機中は 15 秒ごとに状態を更新（868655a） | 0.2.1 |
| 4 | 更新マネージャーの「確定済み」のアイコンが欠ける（Adwaita に `emblem-ok-symbolic` がない） | アプリのアイコンを使う（868655a） | 0.2.1 |
| 5 | 初期設定・ツアーの表示名が「Debian GNU/Linux」 | os-release の NAME／PRETTY_NAME を「QuantumOS 0.x Desktop (Debian 13 base)」に（ID は debian のまま）（868655a） | 0.2.1 の os-release |
| 6 | Dash の端末が出ない（gnome-core が入れるのは gnome-terminal） | お気に入りを `org.gnome.Terminal.desktop` に（868655a） | [23](23-dash-021.png) |
| 7 | 試行待ちのとき、再起動ボタンが画面外 | 見出しの横にも再起動ボタンを出す（127dbee） | 未確認（0.2.1 では試行待ちにしていない） |
| 8 | ファイル選択画面に使っていない側のルートスロットがマウント可能なボリュームとして出る | ESP・ルート A/B・状態領域を udisks から隠す udev 規則を追加 | 実行中の VM に同じ規則を置き、4 パーティションとも `HintIgnore: true` を確認 |
| 9 | 更新で書かれた起動エントリーにコンソール指定（`console=tty1 console=ttyS0`）がない（**標準版にもある既存の不具合**。`kernel_cmdline_extra` の既定値が `quiet` のため） | 標準版・デスクトップ版の `update.conf` に `make-disk.sh` と同じ値を設定。試験で一致を確認 | 次の更新で確認が必要 |

## 観察事項（未修正）

1. **更新バンドルはルートイメージ全体を無圧縮で含む。** デスクトップ版は 9 GiB のイメージで 9.7 GB になったため、ルートイメージを 6 GiB に縮めた（6.5 GB）。圧縮または差分のバンドル形式は今後の課題。
2. **GNOME ソフトウェアが「アップデートが古くなっています」と通知する。** ネットワークのない VM で更新確認が一度も成功していないため。OS のパッケージは扱わない（PackageKit は停止）。Flatpak のリモートを登録した環境での挙動は未確認。
3. **GDM の利用者一覧。** 初回は「一覧にない場合」の入力欄が開いた（評価時のクリック位置による可能性がある）。
4. **IntelliJ IDEA の利用規約以降の初回設定と、IDE 内でのプラグインの動作は未確認。** 同意していないため（プラグイン自体は以前の評価で確認済み）。

## 評価環境に起因する事項

- **llvmpipe（Mesa のソフトウェア描画）が TCG で segfault した。** AVX 命令を使う CPU モデル（`-cpu Haswell-noTSX`）で、GNOME Shell が起動を繰り返していた。AVX のない `-cpu Westmere` と単一スレッド TCG で安定した。マルチスレッド TCG では qemu 自体が異常終了した（qemu 8.2.2）。
- エミュレーションのため、ログインからデスクトップ表示まで 2〜3 分、アプリの起動に 1〜2 分かかる。実機（KVM／GPU あり）での性能評価は未実施。
- ファイルの受け渡しは、ファイルを USB ストレージとしてホットプラグして行った（VM はネットワークなし）。
