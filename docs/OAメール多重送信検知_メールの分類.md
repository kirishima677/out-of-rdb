# OAメール多重送信検知：メールの分類

`Email and Notifications - Emails_new`（全44行）の各メールを、検知の仕組みがどう扱うかに分類したもの。
検知の仕様そのものは `OAメール多重送信検知_本文重複_仕様.md` を参照。

---

## 前提：何を「重複」と呼ぶか

| 項目 | 値 |
| --- | --- |
| 判定キー | **宛先 + 本文**（それぞれハッシュ化） |
| 判定窓 | 1時間 |
| 閾値 | 同じキーが **2件以上** |

**同じ人に、同じ内容が、1時間以内に2通以上**届いたときだけ異常とみなす。
一斉配信（同じ本文を多数の宛先へ）は宛先が違うため、原理的に検知対象にならない。

---

## 分類の軸は一覧表の2列で決まる

| 列 | 何を決めるか |
| --- | --- |
| **Sending Schedule** | **検知できるかどうか**。`Immediately` は worker を通らず送信ログが出ない |
| **Email Creation Schedule** | **誤検知の起こりやすさ**。定期バッチか、ユーザー操作起点か |

この2列だけで4グループに分かれる。

| グループ | 見分け方 | 件数 | 検知での扱い |
| --- | --- | --- | --- |
| **A** | Sending = `Immediately` | 4 | **検知対象外**（ログが出ない） |
| **B** | Creation = `Every 5 mins` | 16 | **鳴らない**（本文が毎回変わる） |
| **C** | Creation = `Daily` / `Weekly Scheduled` | 3 | 窓の外。**ただし1件は例外** |
| **D** | Creation = `On Trigger` | 18 | **本来の検知対象** |
| — | 一覧に記載が無い | 3 | 未確認 |

---

## A：検知対象外（4件）

| Email Type | Target | Creation | Sending |
| --- | --- | --- | --- |
| New Registration | Alumni/Employee | On Trigger | **Immediately** |
| **Forgot Password** | Alumni/Employee | On Trigger | **Immediately** |
| **Forgot Password** | Admin | On Trigger | **Immediately** |
| Connecting an account | Alumni/Employee | On Trigger | **Immediately** |

**worker（`EvenSendEmails` / `OddSendEmails`）を通らないため、`success sending email:` のログが出ない。**
何通送られても検知側からは見えない。

コード側でも裏が取れている。

| 経路 | 状態 |
| --- | --- |
| `OAResetPassword` | `emails` に行は作るが `send_status = 'cancelled'`。worker は送らず、Laravel の Notification が直接送信する |
| `ForgotPasswordController:160` | `Mail::send` で直接送信。`emails` に行を作らない |

仮にログが出ていたとしても鳴らない。リセットURLにはワンタイムトークンと `encrypt($email)` が含まれ、**どちらも毎回異なる値**になるためである。

> **「ユーザーが同じ操作を連打すると同じ本文が複数通出るのでは」という懸念に対しては心配ない。**
> ただし裏を返すと、**この4経路で本当に暴走が起きても検知できない。**

---

## B：鳴らない（16件）

5分間隔の定期バッチで生成されるもの。件数が最も多く、送信頻度も高い。

| Email Type | Target | Template |
| --- | --- | --- |
| Verified | Alumni | `verified` |
| Room Approval | Alumni/Employee | `room_approved` |
| Room Follow Request | Alumni/Employee | `room_follow_request` |
| Approval | Alumni/Employee | `room_follow_approved` |
| Room Invite (From Admin) | Alumni/Employee | `room_invite` |
| Mention (Feed) | Alumni/Employee | `mention` |
| Mention (Room) | Alumni/Employee | `mention` |
| @all Mention (Room) | Alumni/Employee | `mention` |
| comment (followedRoom) | Alumni/Employee | `mention` |
| Add alumni tag | Alumni/Employee | `generic_notification` |
| New Post from Alumni/Employee | Alumni/Employee | `new_post` |
| New Post from Company | Alumni/Employee | `new_post` |
| (HKZ) | Alumni/Employee | — |
| Event Participate | Alumni/Employee | `generic_notification` |
| Poll Answer | Alumni/Employee | `generic_notification` |
| New Room Messages | Admin | `new_room_messages` |

**本文にメッセージ内容と発言時刻が含まれるため、毎回ハッシュが変わる。**

実測では、ある受信者が「メッセージが届きました」を50時間で **104通**、最短4分間隔で受け取っていた。本文ハッシュを比較したところ **5件すべて異なった**（本文長も 15,769〜18,421 とばらつく）。

| 判定方法 | この経路での結果 |
| --- | --- |
| 件名で判定 | **毎時鳴り続ける。使えない** |
| 本文で判定 | **鳴らない。正しい** |

**件名ではなく本文で判定している理由がここにある。**

加えて、5分間隔の送信系はすべて再送防止のガードを持つ。未読が続いてもメールは再送されない。

| 経路 | 再送防止 |
| --- | --- |
| `NewMessagesEmailer` | `message_recipients.is_emailed = 1` |
| `NewMessagesAdminEmailer` | 同上 |
| `NotificationsEmailer` | `notification.is_emailed = true` |
| `NewGroupChatAdminMessagesEmailer` | `last_read_date` を前進 |

---

## C：既知バグはここにいる（3件）

| Email Type | Target | Creation | Template |
| --- | --- | --- | --- |
| **Alumni Not Done with Welcome Questions** | Admin | **Daily Scheduled / 9AM JPT** | `generic_notification` |
| Digest_weekly | Alumni/Employee | Weekly Scheduled / 9AM JPT | `daily_digest` |
| Digest | Admin | Weekly Scheduled / 9AM JPT | `admin_digest` |

### 1行目が `inform-admin:registrants-status` である

日次なので本来は判定窓1時間の外だが、**実際の実行は9時台から14時台まで分散し、その中で同じ宛先へ同じ本文が複数通出る**。2026-09-17 の調査では、1日に **3通** 受け取っている受信者を実データで確認した。

修正されるまでの間、毎朝この検知が鳴り続ける。そのため**件名のハッシュで通知だけを抑止する**。記録は通常どおり残すため、修正後に発生状況を遡って確認できる。

除外（記録しない）ではなく抑止（記録するが通知しない）にしているのは、直ったことを検知の記録側で確認したいからである。

### Digest 2件は窓に入らない

週次のため、1時間の判定窓には収まらない。
なおテンプレート名が `daily_digest` だが実際の起動は週次である。**名前と動作が食い違っているので、調査時に混乱しやすい。**

---

## D：本来の検知対象（18件）

ユーザー操作・管理者操作を起点に1通ずつ出るもの。**同じ宛先へ同じ本文が1時間以内に2通出たら異常**である。

| Email Type | Target | Template |
| --- | --- | --- |
| Change Email | Alumni | `change_email` |
| New Message | Alumni/Employee | `new_dm` |
| Alumni Invite | Alumni | `generic_notification` |
| Account Deletion | Alumni/Employee | `generic_notification` |
| **Welcome Question Reminder** | Alumni | `reminder` |
| Switch to Alumni | Employee | `generic_notification` |
| Switch to Employee | Alumni | `generic_notification` |
| Event Details Change | Alumni/Employee | `generic_notification` |
| Room Approval | Admin | `room_approval` |
| Room Follow Request | Admin | `room_follow_request_admin` |
| Change Email | Admin | `change_email` |
| New Message (1-1) | Admin | `new_dm` |
| New Alumni Registrant (Done with Welcome Questions) | Admin | `generic_notification` |
| New Post from Alumni/Employee | Admin | `new_post_admin` |
| Event Join | Admin | `generic_notification` |
| Poll Answer | Admin | `generic_notification` |
| New Company Registrant | Other | `prereg_admin_notif` |
| Company Registration | Other | `prereg_company_notif` |

### 注意が要る1件

**Welcome Question Reminder** だけ Creation が `On Trigger; 9AM JPT` と特殊である。「Company Admin sends reminder via Alumni List」が起点なので、**管理者が同じ操作を2回行えば同じ本文が2通出る**。これは不具合ではなく操作どおりの動作だが、検知は鳴る。

鳴った場合は `created_at` の間隔を見れば、操作起因（数分以上あく）か暴走（ほぼ同時刻）かを切り分けられる。

---

## 一覧に記載が無い（3件）

| Email Type | Target |
| --- | --- |
| Event Unparticipate | Alumni/Employee |
| Event Unparticipate | Admin |
| report is ready for download | Other |

スケジュール列が空のため、**どのグループに入るか未確認。** 検知が鳴ったときに初めて分かる。

---

## `generic_notification` の使い回しに注意

**12件が `emails.redesign.generic_notification` を共有している。** 件名と本文で内容を分ける作りである。

抑止は件名のハッシュで行うため、**抑止対象を追加するときは、その件名が他の通知と重複していないか確認する必要がある。** 確認を怠ると、関係のない通知まで通知が止まる。

現在の抑止対象（`Alumni Not Done with Welcome Questions`）の件名は他と重複しないため、現時点では問題ない。

---

## 判定の流れ

```
Sending Schedule は Immediately？
│
├ はい ──────────────────────→ 検知対象外（A）
│
└ いいえ（1 Minute Interval）
   │
   ├ 同じ宛先・同じ本文が1時間で2件以上？
   │   │
   │   ├ ならない ───────────→ 正常（B はここに落ちる）
   │   │
   │   └ なる
   │       │
   │       ├ 抑止リストの件名？
   │       │   │
   │       │   ├ はい ───────→ 記録するが通知しない（C の1件）
   │       │   │
   │       │   └ いいえ ─────→ Slack へ通知（D）
```

---

## 検知できないもの

| 事象 | 理由 |
| --- | --- |
| 1時間を超えて離れた重複 | 判定窓の外 |
| 一斉配信 | 判定キーに宛先を含むため、そもそも異常にならない |
| 同じ宛先への異なる本文 | 本文が違えばキーが変わる |
| Sending = `Immediately` のメール | 送信ログが出ない（グループA） |
| 送られるべきメールが送られていない | 本仕様の対象外 |

## 誤検知の要因

| 要因 | 対応 |
| --- | --- |
| ログの再送（Fluent Bit） | `logTimestamp` の優先順位を修正する |
| 同一文面の連投 | 対応しない（成立条件が4つ重なる必要があり、既読が付けば送信自体が止まる） |
| `inform-admin:registrants-status` | 通知を抑止する（グループC） |
| 管理者によるリマインドの二度押し | 対応しない（`created_at` の間隔で切り分ける） |
