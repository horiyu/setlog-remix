# setlog-remix

iOS のショートカットで動画を撮り、GitHub に置いた**フォーマット**（エフェクトと編集の型）で仕上げて
[setlog](https://setlog.kr/) に送る。

フォーマットは JSON で書いて GitHub のリポジトリで公開する。ショートカットにリポジトリ名
（例: [`horiyu/setlog-formats`](https://github.com/horiyu/setlog-formats)）を書いておくと、撮ったあとに
そのリポジトリのフォーマットが一覧で出て、選んだもので描いて投稿する。誰かのフォーマット集を使いたければ
リポジトリ名を書き換えるだけ。自分で作るなら fork して `format.json` を足すだけ。

*Shoot a video with an iOS Shortcut, pick a format from a GitHub repository, and post it to setlog. A format is
a JSON file (clip, framing, effects) published in a repository; the Shortcut names the repository, lists its
formats, and this PC renders the video in the one you pick. setlog only takes what its camera sees, so an
Android emulator on the PC, signed in to your account, plays the result into its camera and posts it.*

```
iPhone ─ビデオを撮影─▶ GET /formats?repo=owner/name ─▶ 一覧から選ぶ ─▶ POST /log（動画＋format＋一言＋ルーム）
                            │                                               │
                     GitHub から format.json を取得                 render.py が 1710x962 に描く
                     （コミットごとに cache/ に保存）                       │
                                                         post.sh: エミュレータのカメラに流す → setlog で撮影
                                                                  → 一言を貼る → ルームにチェック → 送信 → 電源オフ
```

## 使う前に（重要）

- setlog の利用規約（第3条）は、運営会社の許可なく自動化プログラムを使うことを禁じている。この仕組みは
  エミュレータ上の setlog の撮影・送信をスクリプトで操作するので、**使う前に運営会社（New Chat）の許可を得ること**。
  作者は自分の利用について許可を得ているが、それはあなたの利用を許可するものではない。
- setlog は「その場で撮る」アプリ。加工した動画を送るのはアプリの趣旨から外れうるので、友人のいるルームに
  送るときは、そういう Log だと相手に伝えておくこと。
- 作者の個人的な道具で、setlog・New Chat とは無関係。無保証。

## フォーマット

書き方は [horiyu/setlog-formats の README](https://github.com/horiyu/setlog-formats#フォーマットの作り方) が正。
`formats/<id>/format.json` に、使う秒数（`clip`）、枠への収め方（`frame`）、エフェクトの列（`effects`）を書く。

**フォーマットはデータで、コードではない。** 他人のリポジトリを指しても、この PC で任意のコマンドが走ることはない:

- エフェクトは `render.py` の `EFFECTS` にある決まった種類だけ。数値は範囲を検査し、知らないキーはエラー。
- フォーマットが使うファイル（LUT・画像・フォント）はそのリポジトリの中に限り、作業用フォルダに決まった名前で
  コピーしてから使う。リポジトリ由来の文字列やパスを ffmpeg のフィルタに埋め込まない。
- 文字は Pillow で PNG に描いて重ねる（ffmpeg の drawtext は使わない）。
- tarball は通常ファイルとフォルダだけを展開し、外へは書かない。大きさにも上限がある。
- `ALLOWED_REPOS` で使えるリポジトリを絞れる。

## 仕組み

| ファイル | 役割 |
|---|---|
| `server.py` | 受け口。systemd のソケット起動で必要なときだけ立ち上がり、無通信が続くと終わる（常駐しない） |
| `repo.py` | GitHub からリポジトリを取ってくる（コミット単位で `cache/` に、直近 3 つ）。フォーマットの一覧 |
| `render.py` | format.json を読んで ffmpeg で描く。`check` / `render` / `sheet` はフォーマット作者の手元確認用 |
| `worker.sh` / `post.sh` | キューを1件ずつ描いて投稿する。他のエミュレータ利用者とは `EXTRA_LOCKS` で排他 |
| `find_room.py` / `rooms.py` | 送信画面でルームをアバター画像で探す（`rooms/<ルーム名>.png`、`bin/add-rooms.sh` で切り出す） |
| `bin/` | SDK の用意、エミュレータの作成・起動・正しい電源オフ、ルームの登録、受け口の設置、点検（`doctor.sh`） |
| `lib/clip.py` | 一言を X のクリップボード経由でエミュレータに渡す（日本語は `adb shell input text` を通らない） |
| `systemd/` | ユーザー単位の systemd ユニットの雛形（`bin/install-service.sh` が入れる） |

エミュレータのカメラは 1710x1280 の枠を読み、setlog はその上端の 16:9 帯（1710x962）を使う。出来上がりはちょうど
その帯の大きさなので、`post.sh` は上に置いて下を埋めるだけ。setlog がカメラを開いてから撮り始めるまでの約 5 秒は
先頭フレームの静止（`LEAD`）で吸収するので、Log は出来上がりの 0 秒目から始まり、2 秒ちょっと残る。

## 必要なもの

- Linux の PC（X のデスクトップと KVM）。Android SDK と JDK は `bin/setup-sdk.sh` が `sdk/` `jdk/` に入れる
  （既にあるならシンボリックリンクでいい）
- Python 3.10 以上と Pillow・python-xlib、`ffmpeg`、日本語フォント:
  `sudo apt install python3-pil python3-xlib ffmpeg fonts-noto-cjk`
- setlog（エミュレータの中で Google Play から、または自分の端末から抜いた APK を `apk/` に）
- Tailscale（PC と iPhone が同じ tailnet）
- private なフォーマット集を使うなら GitHub のトークン（`settings.conf` の `GITHUB_TOKEN`、なければ `gh auth token` を試す）

## セットアップ

1. `settings.conf.example` を `settings.conf` にコピーして編集する（初回は自動でコピーされる）。
   `DEFAULT_REPO` にいつものフォーマット集。同じ PC で他にもエミュレータを動かすものがあれば、その
   ロックファイルを `EXTRA_LOCKS` に（エミュレータは同時に1台しか動かせない）。
2. `bin/setup-sdk.sh` → `bin/doctor.sh` で足りないものを確かめる。
3. エミュレータを作って setlog を入れる: `bin/make-avd.sh`。開いた setlog に**スマホと同じ方法で**サインインし、
   「Transfer from another device」を選んでスマホで承認し、暗号鍵を移す。既存のアカウントで
   「Create a new encryption key」は押さないこと（古い Log が読めなくなりうる）。終わったら `bin/stop-emu.sh`。
4. 送り先のルームを登録する: `bin/add-rooms.sh`（何も送らずに送信画面の行を切り出す）→
   `state/rooms-new/sheet.png` を見て `bin/name-room.sh <番号> "<ルーム名>"`。
5. 受け口を入れる: `bin/install-service.sh`。合言葉（`state/token`）を作り、systemd のソケットと
   `tailscale serve --https=8451` を設定して、ショートカットに書く URL と合言葉を表示する。

## iOS ショートカット

上から順に並べる。`<受け口>` は `https://<machine>.<tailnet>.ts.net:8451`、`<合言葉>` は `state/token` の中身。
変数は、タップして「変数を選択」からどのアクションの結果かを指で選ぶと取り違えない。

1. **テキスト**: `horiyu/setlog-formats`（使うフォーマット集。`owner/name` か `owner/name@ブランチ`）
2. **URL エンコード**: 入力は 1 のテキスト
3. **ビデオを撮影**: カメラ「背面」
4. **メディアをエンコード**: 入力は 3 の「ビデオ」、サイズ 1920×1080（送る量を減らす。無くても動く）
5. **URL の内容を取得**: `<受け口>/formats?token=<合言葉>&repo=`［2 の結果］
6. **辞書の値を取得**: キー `choices`、入力は 5 の結果
7. **リストから選択**: 入力は 6 の結果、プロンプト「フォーマット」
8. **URL エンコード**: 入力は 7 の「選択中の項目」
9. **URL の内容を取得**: `<受け口>/rooms?token=<合言葉>`
10. **辞書の値を取得**: キー `rooms`、入力は 9 の結果
11. **リストから選択**: 入力は 10 の結果、「複数を選択」オン
12. **テキストを結合**: 入力は 11 の「選択中の項目」、区切り「カスタム」`,`
13. **URL エンコード**: 入力は 12 の「結合済みのテキスト」
14. **テキストを要求**: 「一言」 → **URL エンコード**
15. **URL の内容を取得**: 方法 POST、本文を要求「ファイル」に 4 の「エンコード済みのメディア」。URL は
    `<受け口>/log?token=<合言葉>&repo=`［2］`&format=`［8］`&room=`［13］`&caption=`［14 の URL エンコード］
16. **辞書の値を取得**: キー `message`、入力は 15 の結果 → **通知を表示**

送る前に仕上がりを見たいときは、15 から先をこう置き換える:

15. **URL の内容を取得**: POST、本文はファイル（4 の結果）、URL は
    `<受け口>/preview?token=<合言葉>&repo=`［2］`&format=`［8］`&caption=`［14］
16. **辞書の値を取得**: キー `video` → **テキスト** `<受け口>`［16 の結果］ → **URL の内容を取得** → **クイックルック**
17. **メニューから選択**: 「送る」「やめる」。「送る」の中で 15 の結果から `job` を取り出し、
    **URL の内容を取得**（POST）`<受け口>/send?token=<合言葉>&job=`［job］`&room=`［13］ → `message` を通知

うまくいかないときは `message` に理由が出る（合言葉が違う、フォーマットが無い、format.json の何行目が変、など）。

- 日本語をヘッダーに入れると iOS が送らないことがある。一言・ルーム・フォーマット名は URL エンコードしてクエリで送る。
- `format` は id（`vhs`）でも一覧の表示名でもいい。一覧を出さずに毎回同じフォーマットで送るなら 5〜8 を消して `format=vhs` と書く。

## 動作の確認と記録

```sh
curl http://127.0.0.1:8091/health
T=$(cat state/token)
curl "http://127.0.0.1:8091/formats?token=$T&repo=horiyu/setlog-formats"
curl -F token=$T -F format=vhs -F caption=テスト -F video=@clip.mov http://127.0.0.1:8091/preview   # 描くだけ
curl -F token=$T -F format=vhs -F room=vlog -F dry=1 -F video=@clip.mov http://127.0.0.1:8091/log   # 送信画面でキャンセル
python3 repo.py horiyu/setlog-formats                  # 一覧と、壊れたフォーマットの理由
tail state/server.log state/worker.log
```

`dry=1` のジョブは送信画面でキャンセルする（`DRY_RUN=1 ./post.sh done/<job>` でも同じ）。各段の画面は
`state/last-send.png`（一言を貼った直後）、`state/last-room.png`（ルームを選んだ直後）、`state/last-sent.png`。
投稿の記録は `state/posts.jsonl`。

## 作者・ライセンス

作: [horiyu](https://github.com/horiyu)。[MIT License](LICENSE)。
