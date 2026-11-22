# 更新・試行起動・切り戻し（OS-06／07）

## ディスク構成

| パーティション | 名前（PARTLABEL） | 用途 |
|---|---|---|
| 1 | `ESP` | systemd-boot、`/quantumos/{a,b}/vmlinuz`・`initrd.img`、エントリー `quantumos-{a,b}.conf` |
| 2 | `qos-root-a` | ルートスロット A（読み取り専用） |
| 3 | `qos-root-b` | ルートスロット B（読み取り専用） |
| 4 | `qos-state` | `/var`：利用者ホーム、`/etc` オーバーレイ、更新状態 |

起動中のスロットはカーネルコマンドラインの `quantumos.slot=a|b` で判別します。

## 状態遷移

```
確定済み(confirmed) ──update──▶ 試行予約(trial_reserved) ──再起動──▶ 試行中(trial_in_progress)
     ▲                              │                                   │
     │                              │ 試行先が判定まで到達しない          ├─ 健全性確認 合格 ─▶ 確定済み
     │                              ▼                                   └─ 不合格・期限超過 ─▶ 切り戻し
     └──────────────────── 切り戻し(rolled_back)（新しい更新を受け付ける）
```

## `qos-admin update BUNDLE`

1. 排他ロックを取る。取れない場合は `UPDATE_IN_PROGRESS`。試行予約・試行中であれば `UPDATE_PENDING`。
2. イメージ内の公開鍵でマニフェストの署名を確認し、その後で製品ID・アーキテクチャ・版・各ファイルのハッシュとサイズ・空き容量を確認する。**ここまでに失敗したら何も変更しない。**
3. 非稼働スロットのエントリーを削除してから、ルートイメージを非稼働ルートに、カーネルと initrd を ESP に書き込む。
4. 書き込んだ内容をもう一度読み、ハッシュを照合する。
5. 照合に通った後でだけ、非稼働スロットのエントリーを作成し、one-shot（次回1回だけ）の起動先にする。状態を「試行予約」にする。
6. **再起動はしない。** 管理者が都合のよい時に再起動する。

## 起動時の判定（`qos-boot-assess.service` → `qos-admin boot-assess`）

| 状態 | 起動したスロット | 処理 |
|---|---|---|
| 試行予約 | 予約時と同じ起動（boot_id が同じ） | 何もしない |
| 試行予約 | 試行先 | 試行中にし、健全性確認を行う。合格したら既定エントリーを試行先にして確定。不合格なら切り戻し |
| 試行予約 | 確定済みスロット | 試行先が起動しなかった：切り戻し（`TRIAL_BOOT_FAILED`） |
| 試行中 | どちらでも | 前回の判定が終わらなかった：切り戻し（`HEALTH_CHECK_INCOMPLETE`） |

切り戻しでは、one-shot を消し、既定エントリーを確定済みスロットにし、失敗コードを記録します。状態領域の利用者データは変更しません。

## 失敗コード

| コード | 意味 |
|---|---|
| `PERMISSION_DENIED` | 管理者権限がない |
| `UPDATE_IN_PROGRESS` | ほかの更新操作が実行中 |
| `UPDATE_PENDING` | 試行予約または試行中の更新がある |
| `BUNDLE_INVALID` | バンドルを開けない、メンバーが不足または想定外 |
| `KEY_UNAVAILABLE` | 公開鍵を読めない |
| `SIGNATURE_INVALID` | マニフェストの署名が正しくない |
| `MANIFEST_INVALID` | マニフェストの形式が不正 |
| `PRODUCT_MISMATCH`／`ARCH_MISMATCH` | 製品IDまたはアーキテクチャが違う |
| `VERSION_REJECTED` | 確定済みの版より新しくない |
| `HASH_MISMATCH` | ファイルのハッシュまたはサイズがマニフェストと違う |
| `INSUFFICIENT_SPACE` | スロットまたは ESP の容量不足 |
| `SLOT_STATE_INVALID` | 起動中のスロットが確定済みスロットでない、または判別できない |
| `WRITE_FAILED`／`VERIFY_FAILED` | 書き込み失敗／書き込み後の照合失敗 |
| `BOOTLOADER_FAILED` | `bootctl` の操作に失敗 |
| `STATE_CORRUPT` | 更新状態ファイルが読めない |
| `TRIAL_BOOT_FAILED` | 試行先が判定まで起動しなかった |
| `HEALTH_CHECK_FAILED`／`HEALTH_CHECK_TIMEOUT`／`HEALTH_CHECK_INCOMPLETE` | 健全性確認の不合格／期限超過／未完了 |

## 診断

```bash
sudo qos-admin update-status
```

```bash
sudo qos-admin service-status
```

```bash
sudo qos-admin health-check
```

```bash
journalctl -u qosd.service
```

qosd のログに残るのは、操作、UID、結果コード、所要時間、命令数、ショット数だけです。回路・測定結果・シードは記録しません。
