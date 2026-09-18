# OA週次ダイジェスト送信の仕組み

> 2026-09-17 のコード調査。「ダイジェストは他と仕組みが違う」という話の裏取りとして実施した。
> 対象は `official-alumni` リポジトリ。

## 結論

**違う。他の通知メールと3点で異なる。**

1. コマンドは処理を行わず、**キューへジョブを投げるだけ**
2. 送信時刻が **09:00 JST に固定**される（他は即時）
3. 重複防止が **キューの `ShouldBeUnique`** に依存しており、最終段には掛かっていない

---

## 起動スケジュール

| コマンド | 起動 | 対象 |
| --- | --- | --- |
| `send_emails:admin_weekly_digest` | **日曜 03:00 JST** | 管理者 |
| `send_emails:alumni_weekly_digest` | **日曜 04:00 JST** | アルムナイ |

いずれも `weeklyOn(0, ...)` + `->timezone('Asia/Tokyo')`。`0` は日曜である。

`send_notifications:view`（月曜 09:00 JST）はダイジェストではなく足あと通知である。**月曜に起動するダイジェストは存在しない。**

> `Digestable::getDates()` のコメントに `Commands will run at Saturday UTC 23:00:00` とあるが、実際は土曜 18:00 / 19:00 UTC である。コメントが古い。
> なお `config/app.php` の `timezone` は `UTC` のため、`now()->isSaturday()` の判定は土曜として真になり、日付計算の分岐は意図どおり動く。

## 処理の流れ

```
コマンド（日曜 04:00 JST）
  └─ dispatch → high キュー
       AlumniWeeklyDigestJob            ShouldQueue + ShouldBeUnique
         └─ RetrievePerCompanyForDigest ShouldQueue + ShouldBeUnique（uniqueId あり）
              会社ごと。アルムナイを 100件ずつ chunk
              └─ ChunkSendingAlumniDigest   ShouldQueue のみ
                   100人ぶんのダイジェストを作成
```

**コマンドが終了した時点では、メールは1通も作られていない。** 実際の生成は Horizon のワーカーが行う。

最終的に `NotifiableTrait::_sendNewEmail()` が `Email` 行を作り、通常どおり `EvenSendEmails` / `OddSendEmails` が送信する。**`emails` テーブルは経由する。**

ただしコマンド開始時の稼働通知（`It has begun in ...` → `issues@hackazouk.com`）だけは `Mail::send` の直送で、`emails` テーブルを通らない。

## 他の通知との違い

| 項目 | 通常の通知 | ダイジェスト |
| --- | --- | --- |
| 実行場所 | コマンド内で完結 | **キュー（3段のジョブ）** |
| 送信時刻 | `Carbon::now()`（即時） | **09:00 JST 固定** |
| 重複防止 | `is_emailed` フラグ | `ShouldBeUnique`（キュー側） |
| 保護の持続 | 永続 | **ジョブ実行中のみ** |
| 送信対象 | 条件に合う全員 | **活動があった人だけ** |

最後の行は `ChunkSendingAlumniDigest::handle()` の判定による。会社・募集・フィード・グループチャット・DM のいずれのセクションにも中身が無い場合、その人にはメールを作らない。

## 送信時刻の決まり方

```php
$this->time_to_send = \Carbon\Carbon::createFromTime(9, 0, 0, 'Asia/Tokyo')->setTimezone('UTC');
```

`ChunkSendingAlumniDigest` のコンストラクタ（`app/Jobs/ChunkSendingAlumniDigest.php:50`）。管理者向けは `AdminWeeklyDigestEmailer.php:230` で `$nineJPT` として作られ、ジョブへ渡される。

**`createFromTime()` は「今日」の 09:00 を作る。** ここでの「今日」は**ジョブが構築された時点**の Asia/Tokyo の日付である。

| ジョブ構築のタイミング | `send_schedule` |
| --- | --- |
| 日曜 04:00 JST（通常） | 日曜 09:00 JST |
| 日曜 09:00 JST を過ぎた後 | 日曜 09:00 JST（**過去**。即時送信される） |
| キューが詰まり月曜へ越えた場合 | **月曜 09:00 JST** |

**キューの滞留状況によって送信日がずれる構造になっている。** 「月曜のダイジェスト」と認識されている場合、ここが原因の可能性がある。

実際の送信曜日はデータで確認できる。件名は `:team_name: 先週のダイジェスト`。

```sql
SELECT DATE(send_schedule) AS d,
       DAYNAME(send_schedule) AS dow,
       TIME(MIN(send_schedule)) AS first_time,
       TIME(MAX(send_schedule)) AS last_time,
       COUNT(*) AS n
FROM emails
WHERE id >= <直近のid> - 300000
  AND subject LIKE '%先週のダイジェスト%'
GROUP BY d, dow
ORDER BY d DESC;
```

`send_schedule` は UTC 保存のため、**09:00 JST は 00:00 UTC** として出る。

## 重複防止の穴

`ChunkSendingAlumniDigest` には **`ShouldBeUnique` が付いていない**。上位2つのジョブのみである。

さらに、このジョブは `DigestChunkLog` を `pending` で作成し、完了時に `completed` へ更新するが、**「既に完了済みなら処理しない」という判定を持たない**。

そのためジョブが途中で失敗して再実行されると、**同じ100人に同じダイジェストがもう1通ずつ作られる**。ダイジェストの内容は対象期間のデータから決まるため、再実行しても本文は同一になる。

発生実績は未確認。次で確認できる。

```sql
SELECT digest_log_id, status, COUNT(*) AS n, SUM(size) AS total
FROM digest_chunk_logs
GROUP BY digest_log_id, status
ORDER BY digest_log_id DESC
LIMIT 20;
```

同じ `digest_log_id` に `pending` が残っていたり、チャンク数が想定を超えていれば再実行の痕跡である。

## 本文重複検知への影響

**ダイジェストの本文には、メール1通ごとに異なる値が埋め込まれている。**

`daily_digest.blade.php` は `@section('email_id', ...)` を持ち、レイアウト `template.blade.php` → `layout/container.blade.php` が開封トラッキング用の画像を出力する。

```blade
<img src="{{ route('open_mail', ['email' => $email, 'email_id' => $em_id_pop, ...]) }}" alt="" style="height: 0px;">
```

URL に `email_id=<数値>` が入るため、**同じ内容のダイジェストでも本文ハッシュは必ず異なる**。

つまり**チャンクジョブの再実行による重複は、素の本文ハッシュでは検知できない**。対処は「本文重複 仕様」を参照。

なお `@if(!$forcePlainText)` で囲まれており、宛先ドメインが `forcedPlainTextEmailDomains` に含まれる場合のみ出力されない。既定では出力される。

## 検知の処理量への影響

ダイジェストは**全員ぶんが同一時刻に送信予約される**。09:00 JST（00:00 UTC）に数千通が一斉に送られるため、その時間帯のログオブジェクトは通常より大きくなる。

平常時の実測は1オブジェクトあたり約350行だが、**日曜の 00:00 UTC 前後はこれを大きく超える可能性がある**。Lambda の処理時間はこの時間帯で実測しておくことが望ましい。

## 未確認事項

- ダイジェストの実際の送信曜日（上記クエリで確認可能）
- チャンクジョブ再実行の発生実績（`digest_chunk_logs` で確認可能）
- 管理者向けダイジェスト（`AdminWeeklyDigestPerCompanyJob`）の詳細。`_sendNewEmail` に `$nineJPT` を渡すところまでは確認済み
- `AdminWeeklyDigestEmailerOld`（405行）の使用状況。Kernel には登録されていない
