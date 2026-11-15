# 使い方 — Raspberry Pi のセットアップから授業での使い方まで

黒板の写真を自動で撮り、授業の終わりに Gemini で要約して PDF にするシステムの手順書です。
上から順番に進めてください。細かい設定や困ったときの対処は `README.md` に書いてあります。

---

## 必要なもの

| もの | 注意点 |
|---|---|
| Raspberry Pi 4 または 5 | メモリ 4GB 以上がおすすめ |
| 公式の電源アダプタ | **Pi 4: 5V 3A / Pi 5: 5V 5A（27W）**。スマホの充電器では電力が足りず、カメラが数枚撮ったあとに止まります |
| microSD カード | 32GB 以上 |
| USB ウェブカメラ | Logicool BRIO など 4K 対応のもの |
| セルフパワーの USB 3 ハブ（あるとよい） | カメラの電力を Pi から取らずに済むので、動作が安定します |
| モニター・キーボード・マウス（または VNC） | カメラの映像ウィンドウを見るために必要 |
| 印刷した ArUco マーカー | `marker_id0.png` を幅 10〜20 cm で印刷 |
| Gemini の APIキー | aistudio.google.com で無料で作れます |

---

## 1. Raspberry Pi OS を入れる

1. PC に **Raspberry Pi Imager** を入れて起動します（raspberrypi.com/software）。
2. 「デバイス」で使う Pi、「OS」で **Raspberry Pi OS (64-bit)**（デスクトップ付き）を選びます。
3. 「次へ」→「設定を編集する」で次を設定します。
   - ホスト名: `raspberrypi`（PC から `raspberrypi.local` で接続できるようになります）
   - ユーザー名とパスワード（忘れないようにメモ）
   - Wi-Fi の名前とパスワード、国は `JP`
   - 「サービス」タブで **SSH を有効にする**
4. microSD に書き込み、Pi に挿して電源を入れます。初回の起動は数分かかります。

起動したら、ターミナルでシステムを最新にします。

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

---

## 2. 必要なソフトを入れる

```bash
sudo apt install -y git python3-venv fonts-noto-cjk fonts-dejavu-core v4l-utils
```

| パッケージ | 用途 |
|---|---|
| `git` | プログラムのダウンロード・更新 |
| `python3-venv` | Python の仮想環境 |
| `fonts-noto-cjk` | PDF に日本語を表示するフォント |
| `fonts-dejavu-core` | PDF に数学記号を表示するフォント |
| `v4l-utils` | カメラの確認用（`v4l2-ctl`） |

---

## 3. プログラムをダウンロードする

```bash
cd ~
git clone https://github.com/echiru-numazu/chalecon.git
cd chalecon
```

あとで最新版にするときは:

```bash
cd ~/chalecon
git pull
```

---

## 4. Python の環境を作る（最初の1回だけ）

```bash
cd ~/chalecon
python3 -m venv myenv
source myenv/bin/activate
pip install --upgrade pip
pip install opencv-contrib-python fpdf2 google-genai pillow
```

- インストールには 5〜10 分ほどかかります。
- **ターミナルを開くたびに** `source myenv/bin/activate` を実行してください。行の先頭に `(myenv)` と出ていれば有効です。
- `opencv-python` ではなく `opencv-contrib-python` を入れてください（マーカー検出に必要）。

---

## 5. Gemini の APIキーを設定する

1. PC のブラウザで **aistudio.google.com** を開き、**Get API key** → **Create API key** でキーを作ります。
2. Pi で、キーだけを書いたファイルを作ります。

```bash
mkdir -p ~/.config
nano ~/.config/gemini_api_key        # キーを貼り付けて Ctrl+O → Enter → Ctrl+X で保存
chmod 600 ~/.config/gemini_api_key   # 自分だけが読めるようにする
```

- この方法なら、ターミナルからでもデスクトップのアイコンからでもキーが読み込まれます。
- **キーをプログラム（.py ファイル）に書かないでください。** GitHub に上げると漏れてしまいます。
- キーが漏れたかもしれないときは、AI Studio でそのキーを削除して作り直してください。

---

## 6. カメラとマーカーを準備する

### カメラをつなぐ

1. カメラを **青い USB 3 ポート**（またはセルフパワーの USB 3 ハブ）に挿します。
2. カメラの名前を確認します。

```bash
ls /dev/v4l/by-id/
```

`...-video-index0` で終わる名前がカメラの映像です。

3. `blackboard_genai.py` と `blackboard_camera.py` の **両方** の `Config` にある `SRC` を、その名前に書き換えます。

```python
SRC = "/dev/v4l/by-id/usb-046d_Logicool_BRIO_F097DC64-video-index0"
```

### マーカーを貼る

- `marker_id0.png` を幅 10〜20 cm で、白い余白ごと、つや消しの紙に印刷します。
- **上下にスライドする黒板の板そのもの** に1枚だけ貼ります（壁や枠ではなく）。
- 黒板が上の位置でも下の位置でも、カメラに映るようにカメラの向きを合わせます。

---

## 7. 環境をチェックする

```bash
cd ~/chalecon
source myenv/bin/activate
python check_setup.py
```

ライブラリ・APIキー・フォント・カメラ・USB の速度・電源を確認し、問題があれば最後に対処方法をまとめて表示します。
**`[ NG ]` がなくなるまで直してから** 次に進んでください。

特に **電源** が `[ NG ]` のときは要注意です。電圧不足になると、カメラが数枚撮ったあとに止まります。公式の電源アダプタに替えるか、カメラをセルフパワーのハブにつないでください。

---

## 8. 撮影だけテストする（任意）

Gemini を使わずに、撮影がうまくいくかだけ確認できます。

```bash
python blackboard_camera.py
```

1. マーカーをカメラの前で **すばやく** 上下に動かし、2秒止めます。
2. ターミナルに `保存成功: ...` と出れば成功です。写真は `~/blackboard_images/` に保存されます。
3. カメラのウィンドウで `q` を押すと終了します。

---

## 9. 授業で使う

### 授業の始めに

```bash
cd ~/chalecon
source myenv/bin/activate
python blackboard_genai.py
```

カメラのウィンドウが開き、左上に状態が表示されます。

| 表示 | 意味 |
|---|---|
| `WAITING FOR ID:0...` | 待機中。黒板が動くのを待っています |
| `BOARD MOVING...` | 黒板が動いている |
| `STOPPED! CHECKING 2.0s` | 黒板が止まった。2秒静止すると撮影します |
| `CAPTURING 4K PHOTO...` | 4K で撮影中 |
| `COOLDOWN 5.0s` | 撮影後の待ち時間 |
| `IMAGES: n` | これまでに撮った枚数 |

### 授業中

何もする必要はありません。先生が黒板をスライドして止めるたびに、自動で写真が撮られます。

- 黒板は **ある程度すばやく** スライドしてください。ゆっくりだと反応しないことがあります。
- マーカーがカメラから見えなくなると、見えるまで一時停止します。

### 授業の終わりに

1. カメラのウィンドウをクリックして **`q`**（または ESC）を押します。
2. 撮った写真がすべて Gemini に送られ、要約されます（10〜60 秒ほど）。
3. ターミナルに `完了！要約レポートを保存しました` と出たら終わりです。

### できあがるファイル

```
~/blackboard_images/Class_YYYYMMDD_HHMM/
  ├─ board_01_....jpg ...       撮影した黒板の写真
  ├─ summary.txt                Gemini の回答そのまま
  ├─ summary_readable.txt       数式を読みやすくしたもの
  └─ Summary_Report.pdf         要約レポート（これを見る）
```

最新のレポートを開くには:

```bash
xdg-open "$(ls -d ~/blackboard_images/Class_* | tail -1)/Summary_Report.pdf"
```

AI の処理に失敗しても、写真はフォルダに残っています。

---

## 10. できたファイルを PC に移す

PC（Linux / Mac / Windows の PowerShell）のターミナルで実行します。`ユーザー名` は手順1で決めた名前です。

```bash
# 最新の授業フォルダを、PC の今いるフォルダにコピー
scp -r ユーザー名@raspberrypi.local:'~/blackboard_images/Class_*' .
```

USB メモリにコピーしたり、Pi のファイルマネージャーから共有ドライブにコピーしても構いません。

---

## 11. 設定を変えたいとき

すべて `blackboard_genai.py`（撮影の設定は `blackboard_camera.py` も）の先頭にある `Config` で変えます。

| やりたいこと | 変える設定 |
|---|---|
| Gemini のモデルを変える | `GEMINI_MODEL` |
| 要約の書き方を変える | `SUMMARY_PROMPT` |
| 黒板をゆっくり動かしても反応させたい | `MOVE_RATIO_THRESHOLD` を小さく（例: `0.02`） |
| 撮影までの静止時間を変える | `STABLE_TIME` |
| 撮影が timeout する・止まる | まず電源を確認。それでもだめなら `KEEP_HIGH_RES = True` |
| 4K が重い・不安定 | `UHD_SIZE = (2560, 1440)` などに下げる |

詳しい説明とトラブルシューティングは `README.md` を見てください。
