# setlog-remix

<p align="center"><img src="docs/all-formats.gif" width="480" alt="全フォーマットの出力をつなげたもの"></p>
<p align="center"><sub>同じ動画を全フォーマットで仕上げたもの（1.2 秒ずつ）。左上から順に VHS・シネマ・モノクロ・タイムラプス・ブーメラン・ドット・雪・花吹雪・シャボン玉・紙吹雪・置き手紙・吹き出し・オーラ・異世界・残像・グリッチ・サーマル・ネオン</sub></p>

iOS のショートカットで動画を撮り、GitHub に置いた**フォーマット**（エフェクトと編集の型）で仕上げて
[setlog](https://setlog.kr/) に送る。

フォーマットは JSON で書いて GitHub のリポジトリで公開する。ショートカットにリポジトリ名
（例: [`horiyu/setlog-formats`](https://github.com/horiyu/setlog-formats)）を書いておくと、撮ったあとに
そのリポジトリのフォーマットが一覧で出て、選んだもので仕上げて投稿する。他の人のフォーマット集を使いたいときは、
リポジトリ名を書き換えるだけでよい。自分で作るなら、fork して `format.json` を足せばよい。

*Shoot a video with an iOS Shortcut, pick a format from a GitHub repository, and post it to setlog. A format is
a JSON file (clip, framing, effects) published in a repository; the Shortcut names the repository, lists its
formats, and this PC renders the video in the one you pick and hands it to
[setlog-post](https://github.com/horiyu/setlog-post), which plays it into the camera of an Android emulator
signed in to your account and posts it.*

```
iPhone ─ビデオを撮影─▶ GET /formats?repo=owner/name ─▶ 一覧から選ぶ ─▶ POST /log（動画＋format＋一言＋ルーム）
                            │                                               │
                     GitHub から format.json を取得                 render.py が 1710x962 の動画に仕上げる
                     （コミットごとに cache/ に保存）                       │
                                                         setlog-post post（エミュレータのカメラで撮影して送信）
```

## 使う前に（重要）

- setlog の利用規約（第3条）は、運営会社の許可なく自動化プログラムを使うことを禁じている。投稿は
  setlog-post がエミュレータ上の setlog をスクリプトで操作して行うので、**使う前に運営会社（New Chat）の許可を得ること**。
  作者は自分の利用については許可を得ているが、その許可はあなたの利用には及ばない。
- setlog は「その場で撮る」アプリ。加工した動画を送るのはアプリの趣旨から外れるおそれがあるので、友人がいるルームに
  送るときは、そのような Log であることを相手に伝えておくこと。
- 作者が個人で作った道具で、setlog・New Chat とは無関係。無保証。

## フォーマット

書き方は [horiyu/setlog-formats の README](https://github.com/horiyu/setlog-formats#フォーマットの作り方) が正。
`formats/<id>/format.json` に、使う秒数（`clip`）、枠への収め方（`frame`）、エフェクトの列（`effects`）を書く。

**フォーマットはデータで、コードではない。** 他人のリポジトリを指定しても、この PC で任意のコマンドが実行されることはない:

- エフェクトは `render.py` の `EFFECTS` にある決まった種類だけ。数値は範囲を検査し、未知のキーはエラーにする。
- フォーマットが使うファイル（LUT・画像・フォント）は、そのリポジトリの中にあるものに限り、作業用フォルダに決まった名前で
  コピーしてから使う。リポジトリ由来の文字列やパスを ffmpeg のフィルタに埋め込むことはない。
- 文字は Pillow で PNG に描画して重ねる（ffmpeg の drawtext は使わない）。
- tarball は通常のファイルとフォルダだけを展開し、展開先の外には書き込まない。サイズにも上限がある。
- `ALLOWED_REPOS` で使えるリポジトリを絞れる。

## 仕組み

| ファイル | 役割 |
|---|---|
| `server.py` | 受け口（PC 側で iPhone からのリクエストを受け付ける部分）。systemd のソケット起動で必要なときだけ立ち上がり、通信がない状態が続くと終了する（常駐しない） |
| `repo.py` | GitHub からリポジトリを取得する（コミット単位で `cache/` に保存し、直近 3 つを残す）。フォーマットの一覧も作る |
| `render.py` | format.json を読んで ffmpeg で動画に仕上げる。`check` / `render` / `sheet` はフォーマット作者の手元確認用 |
| `ar.py` | 映像の中身を見て処理するエフェクト（粒子・貼り付け・吹き出し・オーラ・背景・グリッチ・ネオン・残像）。`render.py` が ffmpeg の処理の合間に呼び出す |
| `worker.sh` | キューのジョブを1件ずつ仕上げ、`setlog-post post` に渡す |
| `bin/` | 受け口の導入（`install-service.sh`）と点検（`doctor.sh`） |
| `systemd/` | ユーザー単位の systemd ユニットの雛形（`bin/install-service.sh` が入れる） |

仕上がりのサイズは 1710x962 で、setlog が Log に使う帯とちょうど同じ大きさなので、setlog-post はそのまま全面に映す。
Log は仕上がった動画の 0 秒目から始まり、2 秒強が残る。

## 必要なもの

- [setlog-post](https://github.com/horiyu/setlog-post) がセットアップ済みで、送り先のルームが登録済みであること
  （エミュレータ・ログイン・ルームの扱いはそちらを参照）
- Python 3.10 以上と Pillow、`ffmpeg`、日本語フォント: `sudo apt install python3-pil ffmpeg fonts-noto-cjk`
- AR のエフェクトを使うなら `bin/setup-ar.sh`（`.venv` に numpy・OpenCV・onnxruntime を入れ、小さなモデル2つを
  `cache/models` にダウンロードする）。**GPU は不要**。人物の切り抜き（MODNet、約25MB）と奥行き推定（Depth Anything V2
  Small の量子化版、約27MB）を、縮小した映像に対して CPU で実行するので、一般的なノート PC でも 3 秒の動画を 30 秒ほどで仕上げられる
- Tailscale（PC と iPhone が同じ tailnet）
- 非公開のフォーマット集を使う場合は GitHub のトークン（`settings.conf` の `GITHUB_TOKEN`。未設定なら `gh auth token` を試す）

## セットアップ

1. `settings.conf.example` を `settings.conf` にコピーして編集する（初回は自動でコピーされる）。
   `DEFAULT_REPO` には普段使うフォーマット集を書く。setlog-post が `~/dev/setlog-post` 以外の場所にある場合は `SETLOG_POST` も設定する。
2. 受け口を入れる: `bin/install-service.sh`。合言葉（`state/token`）を作り、systemd のソケットと
   `tailscale serve --https=8451` を設定して、ショートカットに入力する URL と合言葉を表示する。
3. AR のエフェクトを使うなら `bin/setup-ar.sh`。
4. `bin/doctor.sh` で、必要なものがそろっているか確認する。

## iOS ショートカット

**取り込むだけで使える:** [setlog-remix_public](https://www.icloud.com/shortcuts/91856514ccfe4621b20477b013d45921)。
実行すると、受け口の URL・合言葉・フォーマットのリポジトリ名を尋ねられるので、自分の値を入力する
（合言葉などの値は含まれていない。3 つとも空の状態で配布している）。自分で組みたい場合は、下の手順に従う。

アクションを上から順に並べる。`<受け口>` は `https://<machine>.<tailnet>.ts.net:8451`、`<合言葉>` は `state/token` の中身。
変数は、タップして「変数を選択」から、どのアクションの結果かを選んで指定すると取り違えにくい。

1. **テキスト**: `horiyu/setlog-formats`（使うフォーマット集。`owner/name` または `owner/name@ブランチ`）
2. **URL エンコード**: 入力は 1 のテキスト
3. **ビデオを撮影**: カメラ「背面」
4. **メディアをエンコード**: 入力は 3 の「ビデオ」、サイズ 1920×1080（送るデータ量を減らすため。なくても動く）
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
15. **URL の内容を取得**: 方法は POST、「本文を要求」は「ファイル」にして、4 の「エンコード済みのメディア」を指定する。URL は
    `<受け口>/log?token=<合言葉>&repo=`［2］`&format=`［8］`&room=`［13］`&caption=`［14 の URL エンコード］
16. **辞書の値を取得**: キー `message`、入力は 15 の結果 → **通知を表示**

送る前に仕上がりを確認したいときは、15 以降を次のように置き換える:

15. **URL の内容を取得**: POST、本文はファイル（4 の結果）、URL は
    `<受け口>/preview?token=<合言葉>&repo=`［2］`&format=`［8］`&caption=`［14］
16. **辞書の値を取得**: キー `video` → **テキスト** `<受け口>`［16 の結果］ → **URL の内容を取得** → **クイックルック**
17. **メニューから選択**: 「送る」「やめる」。「送る」の中で 15 の結果から `job` を取り出し、
    **URL の内容を取得**（POST）`<受け口>/send?token=<合言葉>&job=`［job］`&room=`［13］ → `message` を通知

うまくいかないときは、`message` に理由が表示される（合言葉が違う、フォーマットが見つからない、format.json に誤りがある、など）。

- ヘッダに日本語を入れると、iOS が送信しないことがある。一言・ルーム・フォーマット名は URL エンコードして、クエリで送る。
- `format` には、id（`vhs`）と一覧の表示名のどちらも指定できる。一覧を出さずに毎回同じフォーマットで送るなら、5〜8 を削除して `format=vhs` と書く。

## 動作の確認と記録

```sh
curl http://127.0.0.1:8091/health
T=$(cat state/token)
curl "http://127.0.0.1:8091/formats?token=$T&repo=horiyu/setlog-formats"
curl -F token=$T -F format=vhs -F caption=テスト -F video=@clip.mov http://127.0.0.1:8091/preview   # 仕上げるだけ
curl -F token=$T -F format=vhs -F room=vlog -F dry=1 -F video=@clip.mov http://127.0.0.1:8091/log   # 送信画面でキャンセル
python3 repo.py horiyu/setlog-formats                  # 一覧と、読み込めないフォーマットの理由
tail state/server.log state/worker.log
```

`dry=1` のジョブは setlog-post が送信画面でキャンセルする。`state/worker.log` に setlog-post のジョブ ID が出るので、
結果は `~/dev/setlog-post/setlog-post status <job>` で確認できる。投稿そのものの記録・画面・失敗の理由は、setlog-post の
`state/` と `done/` にある。前のジョブをもう一度渡すときは `./worker.sh --job done/<job>`。

## 作者・ライセンス

作: [horiyu](https://github.com/horiyu)。[MIT License](LICENSE)。
