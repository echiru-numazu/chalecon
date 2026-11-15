# 黒板自動撮影 ＋ Gemini要約 — セットアップ・使い方ガイド

## ファイル構成

| ファイル | 内容 |
|---|---|
| `blackboard_genai.py` | **メインプログラム。** USBウェブカメラで黒板を監視し、黒板がスライドして止まると自動で4K撮影します。`q` を押すと全画像をGeminiに送り、要約PDFを作成します。 |
| `blackboard_camera.py` | 撮影のみのバージョン（要約なし）。動作テスト用。 |
| `marker_id0.png` | 黒板に貼るArUcoマーカー。 |

---

## 1. セットアップ

### Python環境（最初の1回だけ）

```bash
sudo apt install python3-venv          # Ubuntu + Python 3.14 の場合: python3.14-venv
python3 -m venv myenv
source myenv/bin/activate
pip install --upgrade pip
pip install opencv-contrib-python fpdf2 google-genai pillow
```

- `opencv-python` ではなく **`opencv-contrib-python`** を使ってください。ArUcoマーカー検出は contrib 版にしか入っていません。
- 古い `fpdf` ではなく **`fpdf2`** を使ってください。
- 新しいターミナルを開くたびに `source myenv/bin/activate` を実行してください。

### PDF用フォント

日本語と数学記号を表示するには Unicode フォントが必要です。

```bash
sudo apt install fonts-noto-cjk fonts-dejavu-core
```

インストール済みか確認するには（2行とも `ii` で始まればOK）:

```bash
dpkg -l fonts-noto-cjk fonts-dejavu-core | grep ^ii
fc-list :lang=ja file | head -3      # 日本語フォントのファイルが表示されればOK
```

フォントが見つからないと、実行時に `日本語フォントが見つかりません` と表示され、PDFの日本語が「?」になります。

特定のフォントを使いたい場合は `PDF_FONT_PATH=/path/to/font.ttf` を設定してください。

### Gemini APIキー

1. **aistudio.google.com** を開き、**Get API key** → **Create API key** をクリックします。
2. 環境変数に設定します。**キーをコードに直接書かないでください。**

```bash
export GEMINI_API_KEY="ここにキーを貼り付け"                         # このターミナルのみ
echo 'export GEMINI_API_KEY="ここにキーを貼り付け"' >> ~/.bashrc     # 永続化
source ~/.bashrc                                                     # すぐに読み込む
```

`echo $GEMINI_API_KEY` で確認できます。空行ではなく、キーが表示されればOKです。

**キーのためにコードを編集する必要はありません。** `blackboard_genai.py` の `summarize_with_gemini()` が環境変数 `GEMINI_API_KEY`（または `GOOGLE_API_KEY`）を読み取り、`genai.Client(api_key=...)` に渡します。プログラムを実行するのと同じターミナル（または `~/.bashrc`）でキーを設定してください。

あとでキーを変更・削除するときは、`~/.bashrc` の `export GEMINI_API_KEY=...` の行を編集し、ターミナルを開き直してください。

キーを一度でもコードに書いて git に commit・push した場合は、漏えいしたものとして扱ってください。AI Studio でそのキーを削除し、新しいキーを作成します。ファイルを消しても、git の履歴からキーは消えません。

### Geminiモデル

`blackboard_genai.py` の `Config` にある `GEMINI_MODEL` で指定します。

```python
GEMINI_MODEL = "gemini-3.8-flash"
```

AI Studio に載っているモデル名を使ってください。使えるモデルの一覧は次のコマンドでも確認できます（表示される名前の先頭の `models/` は外して指定します）。

```bash
python -c "from google import genai; [print(m.name) for m in genai.Client().models.list()]"
```

`404 … model not found` エラーは、モデル名が間違っているか、そのモデルが提供終了になったことを意味します。

---

## 2. カメラ

### カメラの確認

```bash
# カメラを名前付きで一覧表示
for d in /sys/class/video4linux/video*; do echo "$(basename $d): $(cat $d/name)"; done

# 固定のデバイス名（再起動や抜き差しでも変わらない）
ls -l /dev/v4l/by-id/

# 任意: 詳細と対応解像度
sudo apt install v4l-utils
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video4 --list-formats-ext    # 3840x2160 があるか確認
```

### カメラの指定

`blackboard_genai.py` と `blackboard_camera.py` の両方の `Config` で `SRC` を設定します。

```python
SRC = "/dev/v4l/by-id/usb-046d_Logicool_BRIO_F097DC64-video-index0"   # Logicool BRIO ウェブカメラ
SRC = 0                                                               # ノートPC内蔵カメラ
```

番号ではなく `/dev/v4l/by-id/...` の名前を使ってください。`/dev/video4` のような番号は検出順に割り当てられるため、再起動や抜き差しで変わることがあります。by-id の名前はカメラのシリアル番号から作られるので変わりません。

### `index` の意味

1台のUSBカメラは、通常いくつかの `/dev/video` デバイスを作ります。`index` はそのカメラ内での番号です。

| 末尾 | 主な中身 |
|---|---|
| `video-index0` | **通常のカラー映像**。これを使います。 |
| `video-index1` | メタデータ（映像なし） |
| `video-index2` | 赤外線カメラ（顔認証用。搭載モデルのみ） |
| `video-index3` | 赤外線のメタデータ |

確認方法: `v4l2-ctl -d /dev/videoN --all | grep -A3 "Device Caps"`。映像が出るデバイスには **Video Capture** と表示されます。

### カメラの簡易テスト

```bash
python -c "import cv2; c=cv2.VideoCapture('/dev/v4l/by-id/usb-046d_Logicool_BRIO_F097DC64-video-index0'); print('OK' if c.read()[0] else 'FAILED')"
```

ウェブカメラは **USB 3 ポート**（青色のことが多い）に挿してください。USB 2 やハブ経由では4Kが使えないことがあります。

---

## 3. ArUcoマーカー

- **マーカーは1枚だけ使います**: 辞書 **4x4_50**、**ID 0**（`marker_id0.png`）。
- **ID 0 を2枚貼らないでください。** 追跡位置が2枚の間で飛び、誤撮影やカウントダウンのリセットの原因になります。
- 幅 **10〜15 cm** 程度で印刷します（実際の教室では 15〜20 cm）。**白い余白**は残し、光沢紙ではなくつや消しの紙に印刷してください。
- 壁や枠ではなく、**スライドする黒板の板そのもの**に貼ってください。上の位置でも下の位置でもカメラに映る必要があります。
- マーカーを検出すると、周りに **緑の枠** と `ID:0 Y:xx.x%` が表示されます。
- 2枚の板が逆方向にスライドする黒板では、片方の板に貼ります。もう片方だけを動かしても撮影されません。

マーカーを作り直すには:

```bash
python -c "import cv2; m=cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), 0, 400); m=cv2.copyMakeBorder(m,50,50,50,50,cv2.BORDER_CONSTANT,value=255); cv2.imwrite('marker_id0.png', m)"
```

---

## 4. 撮影されるタイミング

マーカーの **上下方向** の位置だけを見ます。位置は画面の高さに対する割合（%）で測ります。左右の動きは無視されます。

| 段階 | 表示される STATE | 条件 |
|---|---|---|
| 1. 待機 | `WAITING FOR ID:0...` | マーカーが見えている |
| 2. 移動中 | `BOARD MOVING...` | 連続する2フレーム間でマーカーが **3%より大きく** 動いた |
| 3. 停止 | `STOPPED! CHECKING 2.0s` | 動きが **1.5%以下** になった最初のフレームで、開始位置からの総移動量が **7%以上**。7%未満なら無視されます。 |
| 4. 撮影 | `CAPTURING 4K PHOTO...` | **2秒間** 静止している（1フレームあたり1.5%以下）。それより大きく動くと段階2に戻ります。 |
| 5. クールダウン | `COOLDOWN 5.0s` | 5秒待ってから待機に戻る |

画面上の表示（STATE など）は OpenCV の標準フォントが日本語に対応していないため、英語のままです。

**注意**
- 動きは **フレームごと** に比較するので、黒板は **速く** 動かす必要があります。約30 FPSの場合、動き始めは1秒あたり画面の高さ1つ分くらいが目安です。ゆっくりスライドすると撮影されなかったり、途中で切れて無視されたりします。FPSを下げると遅い動きでも反応します。
- マーカーが画面から外れると、カウントダウンやクールダウンも含めてすべて一時停止し、戻ってくると再開します。
- 黒板を上げてから同じ位置に戻した場合、総移動量はほぼ0なので撮影されません。

### 調整（各ファイル先頭の `Config`）

| 設定 | 初期値 | 意味 |
|---|---|---|
| `MOVE_RATIO_THRESHOLD` | `0.03` | 移動開始と判定する1フレームあたりの移動量 |
| `MIN_TOTAL_MOVE_RATIO` | `0.07` | 必要な最小の総移動量 |
| `STABLE_RATIO_THRESHOLD` | `0.015` | 静止とみなす1フレームあたりの最大移動量 |
| `STABLE_TIME` | `2.0` | 静止していなければならない秒数 |
| `COOLDOWN_TIME` | `5.0` | 撮影後の待ち時間（秒） |
| `TARGET_ARUCO_ID` | `0` | 追跡するマーカーのID |
| `CAPTURE_TIMEOUT` | `5.0` | 4K画像が届くまで待つ最大秒数 |
| `CAPTURE_SKIP_FRAMES` | `2` | 4Kに切り替えた直後に捨てるフレーム数（露出の安定待ち） |
| `OPEN_RETRIES` | `5` | カメラを開き直すときの最大試行回数 |
| `JPEG_QUALITY` | `95` | 保存するJPEGの画質（0〜100） |

### 撮影中の動き

撮影（4Kへの切り替え → 1枚取得 → 監視用解像度に戻す）はカメラ用の別スレッドで、画像の保存もさらに別のスレッドで行います。そのため撮影中もウィンドウは固まらず、`CAPTURING 4K PHOTO...` が表示されたまま操作できます。撮影にかかった時間は毎回ターミナルに `撮影時間: 切替 0.3秒 / 取得 0.6秒 / 復帰 0.3秒` のように表示されるので、遅いときはどの段階かを確認できます。撮影中に `q` を押しても、撮影と保存が終わってから終了します。

---

## 5. 撮影のテスト（`blackboard_camera.py`）

```bash
source myenv/bin/activate
python blackboard_camera.py
```

1. 印刷したマーカー（またはスマホに表示したもの）をウェブカメラの前にかざします。
2. 上か下に **すばやく** 動かし、2秒間止めます。
3. ターミナルに `保存成功: ~/blackboard_images/blackboard_4k_....jpg` と表示されます。
4. カメラのウィンドウで **`q`** を押すと終了します。

写真が4Kか確認します（`(2160, 3840, 3)` と表示されればOK）。

```bash
python -c "import cv2,glob,os; f=max(glob.glob(os.path.expanduser('~/blackboard_images/*.jpg')),key=os.path.getmtime); print(f, cv2.imread(f).shape)"
```

| 症状 | 考えられる原因 |
|---|---|
| 動かしても `WAITING` のまま | マーカーが検出されていない: 明るくする、マーカーを大きくする、白い余白を残す、4x4_50 の ID 0 か確認する |
| ログ: `移動量が小さいため無視しました` | 移動量が7%未満。もっと大きく動かす。 |
| ログ: `カウントダウンをリセットします` が続く | 手ぶれ。紙を何かに押し当てて固定する。 |
| ログ: `4K画像のキャプチャに失敗しました` | カメラから4Kの画像が時間内に届かなかった。下記参照。 |
| ターミナルに `select() timeout` | カメラは開いたが画像が届かない。下記参照。 |
| カメラが開かない | ウェブカメラが接続されていない。`ls /dev/v4l/by-id/` で確認。 |

### `select() timeout` と「4K画像のキャプチャに失敗しました」の対処

どちらも、カメラは開いているのに画像が送られてこないという意味で、よく同時に出ます。撮影時、プログラムはカメラを閉じて 3840x2160 で開き直し、画像を最大 `CAPTURE_TIMEOUT` 秒（初期値5秒）待ちます（`SafeDynamicCamera._grab_photo`）。時間内に届かないと `4K画像のキャプチャに失敗しました` と表示されます。

次の順番で試してください。

**1. カメラを使っている他のアプリを閉じる。** ブラウザのタブ、Zoom、Cheese、前回起動したスクリプトなど（Ctrl+Z はプログラムを終了せず一時停止するだけです）。

```bash
sudo fuser -v /dev/video*       # カメラを使っているプロセスを表示
pkill -f blackboard_            # 残っているスクリプトを終了
```

**2. USBの速度を確認する。** 最も多い原因です。

```bash
lsusb -t
```

`Driver=uvcvideo` の行を探します。**480M** は USB 2 で、4Kは流せません。**5000M** と表示されるまで、ハブを使わず青い USB 3 ポートに挿し直してください。

**3. カメラを抜き差しする、またはドライバを再読み込みする。**

```bash
sudo modprobe -r uvcvideo && sudo modprobe uvcvideo
```

**4. Pythonを使わずにカメラをテストする。**

```bash
DEV=/dev/v4l/by-id/usb-046d_Logicool_BRIO_F097DC64-video-index0
v4l2-ctl -d $DEV --list-formats-ext | grep -A2 MJPG    # 1920x1080 と 3840x2160 があるはず
v4l2-ctl -d $DEV --set-fmt-video=width=1920,height=1080,pixelformat=MJPG --stream-mmap --stream-count=60
v4l2-ctl -d $DEV --set-fmt-video=width=3840,height=2160,pixelformat=MJPG --stream-mmap --stream-count=30
```

| 結果 | 意味 |
|---|---|
| 一覧に `3840x2160` がない | このカメラは4K非対応、または `SRC` が別のカメラを指している |
| 映像が流れる（`<<<<` とfpsの数値が表示される） | カメラは正常。4Kの起動に時間がかかっているので、`Config` の `CAPTURE_TIMEOUT` を増やす。 |
| 止まる・タイムアウトする | USB、電源、ドライバの問題。手順5へ。 |

**5. ドライバの帯域対策を有効にする。** ノートPCや Raspberry Pi でよく効きます。

```bash
sudo modprobe -r uvcvideo && sudo modprobe uvcvideo quirks=128
```

これで直った場合は、永続化します。

```bash
echo "options uvcvideo quirks=128" | sudo tee /etc/modprobe.d/uvcvideo.conf
```

**6. デバイス名を確認する。** `Config` の `SRC` は **`video-index0`** で終わっている必要があります。`index1` はメタデータなので映像は出ません。

**7. Raspberry Pi では電源を確認する。** 公式の電源アダプタを使ってください。`vcgencmd get_throttled` が `0x0` 以外を表示する場合は電力不足で、ウェブカメラが切断されることがあります。

**USB 3 がどうしても使えない場合は、** 両方のファイルの `Config` で撮影解像度を下げます。

```python
UHD_SIZE = (2560, 1440)   # --list-formats-ext にこのサイズがある場合のみ
UHD_SIZE = (1920, 1080)   # 監視用と同じサイズ。解像度の切り替え自体がなくなる
```

**いつ起きるかで原因を絞れます。** 撮影時（4Kへの切り替え時）だけ出るなら手順2・4・5を、起動直後に出るなら手順1・3・6を確認してください。

---

## 6. Geminiと一緒に使う（`blackboard_genai.py`）

```bash
cd ~/chalecon
source myenv/bin/activate
export GEMINI_API_KEY="..."      # ~/.bashrc に設定していない場合
python blackboard_genai.py
```

1. `blackboard_camera.py` と同じ方法で自動撮影されます。左上に `IMAGES: n  [Q] FINISH & SUMMARIZE` と表示されます。
2. 授業が終わったら、カメラのウィンドウで **`q`**（またはESC）を押します。
3. 全画像がGeminiに送られ（約10〜60秒）、PDFが作成されます。

ターミナルの表示:

```
カメラを停止しました。撮影枚数: 3 枚
3 枚の画像をGeminiに送信して要約中...
AI解析完了！PDFを作成中...
完了！要約レポートを保存しました: ...pdf
```

### 出力

```
~/blackboard_images/Class_YYYYMMDD_HHMM/
  ├─ board_1_....jpg ...       4K画像
  ├─ summary.txt               Geminiの生の回答
  ├─ summary_readable.txt      数式を読みやすく変換したもの（\frac{a}{b} → a/b）
  └─ Summary_Report.pdf        最終レポート
```

最新のレポートを開くには:

```bash
xdg-open "$(ls -d ~/blackboard_images/Class_* | tail -1)/Summary_Report.pdf"
```

### 設定

- `SUMMARY_PROMPT`: Geminiに送る指示文。日本語で書かれており、「回答はすべて日本語で」と指定しているので、要約は日本語で返ってきます。まとめ方を変えたいときはこの文を編集してください。
- `GEMINI_MODEL`: 使用するGeminiのモデル（「1. セットアップ」の「Geminiモデル」参照）。

### トラブルシューティング

| メッセージ | 対処 |
|---|---|
| `環境変数 GEMINI_API_KEY が設定されていません` | プログラムを実行するのと同じターミナルでキーを設定する |
| `画像が撮影されなかったため…` | 要約するものがないので、そのまま終了します |
| `AI処理エラー: 404 … model not found` | `GEMINI_MODEL` を AI Studio にある現行のモデルに変更する |
| `AI処理エラー: 429 …` | 無料枠の上限に達した。しばらく待って再実行する。 |
| `AI処理エラー: 400 API key not valid` | キーのコピーミス、またはキーが削除されている |
| `PDF作成エラー` | テキストは `summary.txt` に保存されています |

AIの処理が失敗しても、画像は **消えません**。授業フォルダに残っています。

コードはGoogleの現行ライブラリ `google-genai`（`from google import genai`）を使っています。古い `google-generativeai` パッケージはメンテナンスが終了しているので、インストールしないでください。

---

## 7. Raspberry Piで動かす

コードは Raspberry Pi でも動きます。Linuxのカメラの仕組み（V4L2）を使っているので、ウェブカメラの by-id の名前も Pi で同じになるはずです（`ls /dev/v4l/by-id/` で確認）。

```bash
sudo apt update
sudo apt install python3-venv fonts-noto-cjk fonts-dejavu-core
python3 -m venv myenv
source myenv/bin/activate
pip install opencv-contrib-python fpdf2 google-genai pillow
export GEMINI_API_KEY="..."
```

| 項目 | 注意点 |
|---|---|
| 機種 | Pi 4 または Pi 5 + **64ビット版** Raspberry Pi OS。それより古い機種は4Kには遅すぎます。 |
| USB | 4Kには **青い USB 3 ポート** を使う |
| 電源 | 公式の電源アダプタを使う。ウェブカメラは Pi から給電されます。 |
| 画面 | カメラのウィンドウが表示されるので、モニターか VNC でデスクトップ版OSを使う（SSHのみでは不可） |
| 速度 | 撮影のたびに4Kへ切り替えるので数秒かかることがありますが、その間も画面は固まりません |
| Piカメラモジュール | リボンケーブル式のカメラは `picamera2` が必要で、このコードのままでは動きません |

コードは `git clone` か `scp` で移します。キーはコードに含まれていないので、Pi でも `GEMINI_API_KEY` を設定してください（上の「Gemini APIキー」参照）。
