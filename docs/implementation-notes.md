# 実装メモ（初版 0.1.0）

設計書 QOS-BD／QOS-FD／QOS-IF 0.1 を実装する際に、**解釈が必要だった点**、**暫定で決めた点**、**未確定のまま残した点**をまとめます。
いずれもレビューで確定するまでは暫定です。

## 1. 実装方式

| 項目 | 採用 | 理由 |
|---|---|---|
| 言語 | Python 3 + NumPy（計算コア）＋ cryptography（署名検証） | Debian 標準パッケージだけで動作し、追加ビルドが要らない。QOS-BD は C++ を計算コアの候補としているため、性能評価の結果によっては計算コアだけを置き換える（ワーカープロセスの入出力を境界にしている）。 |
| 計算の実行 | 実行ごとに子プロセス（`quantumos.worker`）を起動 | 期限超過・切断時に子プロセスを強制終了し、**終了を確認してから**計算枠を解放するため（QOS-FD 2.2）。 |
| 乱数 | NumPy PCG64 に 64 ビットのシードを与え、多項分布で復元抽出 | 同一イメージ・同一ビルド・同一環境での再現性を満たす。環境間の一致は保証しない（QOS-FD 2.1）。 |
| シードの自動生成 | `secrets.randbits(64)`（getrandom(2)） | OS の乱数源。実行上限と空き枠の確認後に取得する。 |
| 認可 | `SO_PEERCRED` の UID／GID と `quantum` グループの照合 | 要求本文の自己申告は使わない（QOS-IF 3章）。 |
| ソケット | systemd ソケット起動（root:quantum、0660、親ディレクトリー 0755） | 一般利用者は親ディレクトリーを書き換えられない。 |
| ブートローダー | systemd-boot、試行起動は `LoaderEntryOneShot` | 試行先が起動しない・判定前に再起動した場合、次の起動は自動的に既定（＝確定済み）エントリーに戻る。 |
| 署名 | Ed25519（PEM 公開鍵をイメージに格納） | 署名方式は未確定事項のため暫定。 |

## 2. 設計書の解釈

### 2.1 回路 JSON 版1（OS-03）

- `schema_version` は最初に確認する。整数でない場合は `CIRCUIT_INVALID`、1 以外の整数は `SCHEMA_UNSUPPORTED`。
- 「終端測定が必須」は、**`measure` 命令がちょうど1つあり、それが最後の命令であること**と解釈した。測定対象は1つ以上の重複しない量子ビット。
- 1量子ビットゲート（h/x/z/rx/rz）は量子ビットを1つ、cx は異なる2つ（`[制御, 標的]`）をとる。
- `theta` は rx／rz では必須で有限の数値。他の命令に `theta` があれば未知キーとして拒否する。
- 真偽値は整数として扱わない（`true` は `n_qubits` や量子ビット番号として不正）。
- `validate` は実行上限（20量子ビット）を確認しない。`run` だけが `BACKEND_LIMIT` を返す。

### 2.2 IPC（QOS-IF 3〜4章）

- 検査順序は QOS-FD OS-04 のとおり：認可 → メッセージ（版・op・キー集合）→ 回路 → shots → seed → 実行上限 → 空き枠 → 乱数種 → 計算。
- 未認可の接続からもフレームは1つ読んでから `ACCESS_DENIED` を返す（応答は通常のフレーム形式）。
- 回路の 4 MiB 判定は、要求の中の `circuit` の値が占める**元のバイト列**で行う（再シリアライズ後の長さではない）。
- 不正 UTF-8、BOM、重複キー（全階層）、`NaN`／`Infinity`、倍精度に収まらない数値（`1e999`）、不正なサロゲートを拒否する。
- 1接続で最初の1フレームだけを処理する。応答後、クライアントがすでに送った後続データは処理せずに読み捨ててから閉じる（読み残しがあるまま閉じると AF_UNIX では相手にリセットが届き、応答を失うことがあるため）。
- 計算中にクライアントが切断（またはクライアントが送信側を閉じた）した場合は切断として扱い、ワーカーを止める。
- `status` は `busy`／`ready` だけを返す。OS 全体の健全性判定ではない。

### 2.3 CLI（QOS-IF 2章）

- CLI は回路ファイルを利用者の権限で読み、4 MiB の上限と厳密な JSON 形式を確認したうえで、**ファイルの内容をそのまま**要求に埋め込む。構造の検証はサービスに任せる。
- `--shots`、`--seed` はサービスへ接続する前に CLI 側でも検証する（終了値 2）。
- QOS-IF 0.1 の失敗コード表にない CLI 側の失敗に、次の暫定コードを追加した（終了値 2）。
  - `USAGE_INVALID`：サブコマンドや引数の誤り
  - `FILE_UNREADABLE`：回路ファイルを開けない
- 応答待ちの上限は、実行期限 120 秒＋30 秒。超えた場合は `TRANSPORT_ERROR`。

### 2.4 更新（OS-06）

- マニフェストの「対象版」は**更新後の版**と解釈した。確定済みの版より新しくない版は `VERSION_REJECTED`（ダウングレード防止）。
- バンドルは tar（非圧縮）。メンバーはディスクに展開せずストリームで読む。想定外のメンバー（パス名を細工したものを含む）があれば拒否する。
- 書き込み先エントリーは書き込み前に削除し、書き込み後の再照合が通ってから作り直す。このため書き込みが中断しても試行先が選ばれることはない。
- 健全性確認：ルートが読み取り専用、状態領域に書き込める、管理コンソール（getty@tty1）が稼働中、qosd が稼働中、ソケットが応答する（root からの `ACCESS_DENIED` も「応答あり」とみなす）、`qosd --self-test`（X(0)→measure(0) の決定的回路が `"1"` だけを返す）。
- 切り戻しの後は、自動では再起動しない。次の起動先を確定済みスロットに戻し、`update-status` に失敗コードを記録する。

### 2.5 `/etc` の扱い

ルートは読み取り専用ですが、利用者アカウント（`quantum` グループへの追加など）は書き込みが必要です。
initramfs で状態領域を `/var` にマウントし、`/etc` を `lowerdir=ルートの /etc`、`upperdir=/var/lib/quantumos/etc-overlay/upper` のオーバーレイにしています。

**注意**：オーバーレイの上位層で変更されたファイルは、更新後の新しい `/etc` の同じファイルより優先されます。上位層に置いてよいファイルの範囲（アカウント、machine-id、ホスト名など）を確定する必要があります（§4）。

## 3. 要件・実装・試験の対応

| 要件 | 実装 | 自動テスト |
|---|---|---|
| OS-01 通常起動・起動失敗 | `image/initramfs/quantumos-state`、`image/rootfs-overlay/etc/fstab` | （起動評価は未実施） |
| OS-02 認可・分離 | `service.py` `Authorizer`、`system/systemd/qosd.{socket,service}` | `test_ipc.py::test_access_denied`、`test_body_cannot_claim_identity`、`test_socket_permissions` |
| OS-03 検証 | `circuit.py`、`protocol.py` | `test_circuit_and_simulator.py`、`test_ipc.py::test_request_errors` |
| OS-04 実行制御・計算 | `protocol.py`、`service.py`、`simulator.py`、`worker.py` | `test_check_order_*`、`test_busy_and_timeout_then_recovery`、`test_disconnect_releases_slot` |
| OS-05 結果 | `service.py` `handle_body` | `test_run_example_from_spec`、`test_seed_round_trip` |
| OS-06 更新・切り戻し | `admin/update.py`、`admin/platform.py`、`admin/bundle.py` | `test_update.py` |
| OS-07 診断 | `qos-admin update-status / health-check / service-status`、qosd のログ | `test_update_status_cli` |
| T-OS-03 X(1)→measure(1,0) が「10」 | | `test_x1_measure_10`、`test_run_example_from_spec`、`test_cli.py::test_run_success` |
| T-OS-04 0、2⁵³+1、2⁶⁴−1 の往復 | | `test_seed_round_trip`、`test_seed_ok`、`test_seed_rejected` |
| T-OS-05 BUSY・TIMEOUT・部分結果なし・後続実行 | | `test_busy_and_timeout_then_recovery` |
| T-OS-06 正規更新・改変・書き込み中断・試行失敗 | | `test_update_trial_and_confirm`、`test_rejected_bundles`、`test_interrupted_write_boots_confirmed`、`test_health_check_failure_rolls_back`、`test_trial_never_comes_up` |
| IF 5.1 1接続1フレーム・欠損／超過フレーム・未知版・4 MiB／8 MiB・終了値と出力の分離 | | `test_only_first_frame_is_processed`、`test_bad_frames_close_without_response`、`test_circuit_4mib_limit_*`、`test_8mib_frame_limit`、`test_cli.py` |

T-OS-01、T-OS-02 のうち実機・仮想機上で確認する項目（起動、管理コンソール、ネットワーク待受がないこと）は、イメージの起動評価で実施します。

## 4. 未確定事項（結合前に確定が必要）

設計書に「確定事項」として挙がっているものと、実装中に見つかったものです。

1. 基盤 OS とカーネルの具体版（現在は Debian `trixie` を候補として `image/config.env` に記載）、評価する実機・仮想化環境
2. 更新バンドルの正式な構造、署名方式と鍵の更新手順（現在は tar＋Ed25519、鍵の更新手段なし）
3. 管理 CLI の機械可読出力（現在は `--json` を暫定で提供）
4. ソケットの接続読み取り期限（現在 10 秒）とソケットの管理設定
5. 試行回数・監視期限・永続化方式（現在は one-shot による試行1回、判定全体の期限 300 秒、状態は `/var/lib/quantumos/update/state.json`）
6. 乱数手順の詳細（現在は NumPy PCG64＋多項分布。NumPy の版が変わると同じシードでも結果が変わりうる）
7. 資源上限と性能基準（現在はメモリ 512 MiB を systemd の `MemoryMax` で、実行期限 120 秒をサービスで適用）
8. `/etc` オーバーレイの上位層に置いてよいファイルの範囲（§2.5）
9. CLI 側の失敗コード `USAGE_INVALID`／`FILE_UNREADABLE` の追加可否
10. 物理アクセス保護、ディスク暗号化、Secure Boot（初版の対象外）
