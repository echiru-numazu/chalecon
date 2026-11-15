"""
環境チェックスクリプト
=====================

blackboard_genai.py を動かすのに必要なもの（ライブラリ・APIキー・フォント・カメラ）が
そろっているかを確認します。Raspberry Pi など新しい環境で最初に実行してください。

  source myenv/bin/activate
  python check_setup.py

APIキーそのものは表示しません。
"""

import importlib
import os
import subprocess
import sys
import time

OK = "[ OK ]"
NG = "[ NG ]"
WARN = "[注意]"

problems = []


def report(ok, label, detail="", fix=""):
    print(f"{OK if ok else NG} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append((label, fix))


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ------------------------------------------------------------
print("=== Python ===")
in_venv = sys.prefix != sys.base_prefix
print(f"{OK if in_venv else WARN} {sys.executable}（Python {sys.version.split()[0]}）")
if not in_venv:
    print("       仮想環境が有効になっていません。先に source myenv/bin/activate を実行してください。")

# ------------------------------------------------------------
print("\n=== ライブラリ ===")
PACKAGES = [
    ("cv2", "opencv-contrib-python"),
    ("fpdf", "fpdf2"),
    ("PIL", "pillow"),
    ("google.genai", "google-genai"),
]
for module, pip_name in PACKAGES:
    try:
        importlib.import_module(module)
        report(True, pip_name)
    except ImportError:
        report(False, pip_name, "インストールされていません", f"pip install {pip_name}")

try:
    import cv2
    report(hasattr(cv2, "aruco"), "OpenCV の ArUco 機能", cv2.__version__,
           "pip uninstall -y opencv-python && pip install opencv-contrib-python")
except ImportError:
    pass

# 以降のチェックは blackboard_genai.py の Config を使う
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import blackboard_genai as bg
    Config = bg.Config
except Exception as e:
    print(f"\n{NG} blackboard_genai.py を読み込めませんでした: {e}")
    print("       上のライブラリをインストールしてから、もう一度実行してください。")
    sys.exit(1)

# ------------------------------------------------------------
print("\n=== Gemini APIキー ===")
if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
    report(True, "APIキー", "環境変数から読み込めました")
elif bg.load_api_key():
    report(True, "APIキー", f"ファイル {Config.API_KEY_FILE} から読み込めました")
else:
    report(False, "APIキー", "見つかりません",
           f"export GEMINI_API_KEY=\"キー\" を実行するか、キーだけを書いたファイル {Config.API_KEY_FILE} を作成")
print(f"       使用するモデル: {Config.GEMINI_MODEL}")

# ------------------------------------------------------------
print("\n=== PDF用フォント ===")
jp = [p for p in Config.JAPANESE_FONTS if p and os.path.exists(p)]
system_jp = bg._system_japanese_fonts()
if jp:
    report(True, "日本語フォント", jp[0])
elif system_jp:
    report(True, "日本語フォント", f"{system_jp[0]}（fc-list で自動検出）")
else:
    report(False, "日本語フォント", "見つかりません", "sudo apt install fonts-noto-cjk")
fb = [p for p in Config.FALLBACK_FONTS if os.path.exists(p)]
report(bool(fb), "数学記号用フォント", fb[0] if fb else "見つかりません", "sudo apt install fonts-dejavu-core")

if jp or system_jp:
    try:
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        family = bg._setup_unicode_fonts(pdf)
        pdf.set_font(family, size=12)
        pdf.cell(text="テスト α² ≤ √x")
        report(True, "PDFに日本語を書き込むテスト")
    except Exception as e:
        report(False, "PDFに日本語を書き込むテスト", str(e), "fonts-noto-cjk を入れ直す、または PDF_FONT_PATH で別のフォントを指定")

# ------------------------------------------------------------
print("\n=== カメラ ===")
src = Config.SRC
if isinstance(src, str):
    exists = os.path.exists(src)
    report(exists, "カメラのデバイス", src, "ls /dev/v4l/by-id/ で名前を確認し、Config.SRC を直す（両方のファイル）")
    if not exists:
        print("       見つかったカメラ:")
        for name in sorted(os.listdir("/dev/v4l/by-id")) if os.path.isdir("/dev/v4l/by-id") else []:
            print(f"         /dev/v4l/by-id/{name}")
else:
    exists = True
    print(f"       Config.SRC = {src}")

usb = [line.strip() for line in run(["lsusb", "-t"]).splitlines() if "uvcvideo" in line] if exists else []
if usb:
    speeds = {s for line in usb for s in ("5000M", "10000M", "480M", "12M") if s in line}
    fast = bool(speeds & {"5000M", "10000M"})
    report(fast, "USBの速度", ", ".join(sorted(speeds)) or usb[0],
           "青い USB 3 ポートに直接挿す（480M は USB 2 で4Kが不安定）")

if exists:
    import cv2
    for label, size in (("監視用", Config.FHD_SIZE), ("撮影用", Config.UHD_SIZE)):
        cap = cv2.VideoCapture(src, bg._camera_backend())
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
        t = time.time()
        frame = None
        while time.time() - t < 8 and frame is None:
            ret, f = cap.read()
            if ret:
                frame = f
        elapsed = time.time() - t
        cap.release()
        time.sleep(Config.SWITCH_DELAY)
        if frame is None:
            report(False, f"{label} {size[0]}x{size[1]}", "映像が届きません（timeout）",
                   "README の「select() timeout の対処」を参照。続く場合は Config.KEEP_HIGH_RES = True も試す")
        else:
            h, w = frame.shape[:2]
            report((w, h) == tuple(size), f"{label} {size[0]}x{size[1]}",
                   f"実際: {w}x{h}、最初の画像まで {elapsed:.1f}秒",
                   "カメラがこの解像度に対応していない。Config の解像度を下げる")

# ------------------------------------------------------------
throttled = run(["vcgencmd", "get_throttled"]).strip()
if throttled:
    print("\n=== Raspberry Pi の電源 ===")
    report(throttled.endswith("=0x0"), "電源", throttled,
           "公式の電源アダプタ（Pi 4: 5V 3A / Pi 5: 5V 5A）を使う。ウェブカメラはセルフパワーのUSB 3ハブ経由にする")
    try:
        bits = int(throttled.split("=")[1], 16)
    except (IndexError, ValueError):
        bits = 0
    MEANINGS = [
        (0, "今まさに電圧不足"), (1, "今まさに周波数制限中"), (2, "今まさに速度制限中"), (3, "今まさに温度制限中"),
        (16, "起動後に電圧不足が起きた"), (17, "起動後に周波数制限が起きた"),
        (18, "起動後に速度制限が起きた"), (19, "起動後に温度制限が起きた"),
    ]
    for bit, text in MEANINGS:
        if bits & (1 << bit):
            print(f"       ・{text}")
    if bits & ((1 << 0) | (1 << 16)):
        print("       電圧不足になると、USBカメラが数枚撮ったあとに切断・停止することがあります。")

# ------------------------------------------------------------
print("\n=== 結果 ===")
if not problems:
    print("すべて問題ありません。python blackboard_genai.py で起動できます。")
else:
    print(f"{len(problems)} 件の問題があります。次の対処をしてください:")
    for label, fix in problems:
        print(f"  ・{label}: {fix}")
