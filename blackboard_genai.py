"""
黒板自動撮影 ＋ Gemini要約 ＋ PDFレポート生成システム
=====================================================

blackboard_camera に Gemini による要約と PDF 出力を加えたプログラムです。

【処理の流れ】
  1. USBカメラで黒板（上下スライド式）に貼ったArUcoマーカーを監視する（FHD）
  2. 黒板が上下に動いて止まったことを検知したら、自動で4K撮影して保存する
  3. 'q' キー（またはESC）で授業を終了すると、撮影した全画像をGeminiに送って要約する
  4. 要約に含まれるLaTeX数式を「人が読める形」（例: \\frac{a}{b} → a/b, x^2 → x²）に変換し、
     Markdownの装飾（#見出し・**太字**・箇条書き）も整えてPDFに出力する

【必要なライブラリ】
  pip install opencv-contrib-python fpdf2 google-genai pillow
  ※ Gemini は新しい公式SDK google-genai を使用します（旧 google-generativeai は不要）
  ※ ArUco検出には opencv-contrib-python が必要です
  ※ PDF出力は fpdf2 を使用します（旧 PyFPDF ではなく fpdf2 をインストールしてください）

【APIキーの設定】
  APIキーはソースコードに直接書かず、環境変数 GEMINI_API_KEY で渡してください。
    Linux / Mac : export GEMINI_API_KEY="あなたのキー"
    Windows     : setx GEMINI_API_KEY "あなたのキー"   （設定後にターミナルを開き直す）

【保存先】
  ~/blackboard_images/Class_YYYYMMDD_HHMM/
    ├─ board_1_YYYYMMDD_HHMMSS.jpg ...   撮影した4K画像
    ├─ summary.txt                        Geminiの生の出力（LaTeXそのまま）
    ├─ summary_readable.txt               LaTeXを読みやすく変換したテキスト
    └─ Summary_Report.pdf                 最終的なPDFレポート
"""

import os
import re
import cv2
import time
import platform
import threading
import logging
from datetime import datetime
from enum import Enum, auto

import PIL.Image
from google import genai
from fpdf import FPDF

# Qt系の不要なログ出力を抑制
os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.qpa.*=false"

# ログの設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
# PDF作成時に fontTools が大量に出す INFO ログを抑制
logging.getLogger("fontTools").setLevel(logging.WARNING)


# ==========================================
# 設定パラメータ（現場に合わせて調整）
# ==========================================
class Config:
    # --- カメラ ---
    SRC = "/dev/v4l/by-id/usb-046d_Logicool_BRIO_F097DC64-video-index0"  # Logicool BRIO ウェブカメラ（固定のデバイス名。ノートPC内蔵カメラは 0）
    FHD_SIZE = (1920, 1080)  # 監視用の解像度
    UHD_SIZE = (3840, 2160)  # 撮影用の解像度
    FPS = 30

    TARGET_ARUCO_ID = 0            # 反応させるArUcoマーカーのID

    # --- 誤動作防止パラメータ ---
    MOVE_RATIO_THRESHOLD = 0.03    # 1フレームあたりの移動判定閾値（3%）
    MIN_TOTAL_MOVE_RATIO = 0.07    # 撮影に必要な「総移動量」（画面高の7%以上動かないと無視）
    STABLE_RATIO_THRESHOLD = 0.015 # 静止と判定するブレ幅（1.5%以下で静止とみなす）
    STABLE_TIME = 2.0              # 静止確認時間（秒）
    COOLDOWN_TIME = 5.0            # 撮影後のクールダウン時間（秒）

    # --- 撮影の速度・安定性 ---
    CAPTURE_TIMEOUT = 5.0          # 4K画像が届くまで待つ最大時間（秒）
    CAPTURE_SKIP_FRAMES = 2        # 切り替え直後に捨てるフレーム数（露出が安定するまで）
    OPEN_RETRIES = 5               # カメラを開き直すときの最大試行回数
    JPEG_QUALITY = 95              # 保存するJPEGの画質（0〜100）

    # --- 保存先 ---
    BASE_SAVE_DIR = os.path.expanduser("~/blackboard_images")

    # --- Gemini（モデルと指示文） ---
    GEMINI_MODEL = "gemini-3.8-flash"
    SUMMARY_PROMPT = (
        "これは授業中に撮影した黒板の画像です。内容を要約し、"
        "箇条書きで整理して、試験の復習に使いやすいように構成してください。"
        "回答はすべて日本語で書いてください。"
    )

    # --- PDF用フォント ---
    # 日本語・ギリシャ文字・数学記号を表示するため Unicode 対応フォントを使用する。
    # 上から順に探し、最初に見つかったものをメイン、残りを不足文字の代替フォントとして使う。
    # 環境変数 PDF_FONT_PATH で任意のフォントを最優先で指定することもできる。
    FONT_CANDIDATES = [
        os.environ.get("PDF_FONT_PATH", ""),
        # Linux（Raspberry Pi など）: sudo apt install fonts-ipafont-gothic fonts-dejavu-core
        "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
        "/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf",
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        # Windows
        "C:/Windows/Fonts/YuGothR.ttc",
        "C:/Windows/Fonts/meiryo.ttc",
        "C:/Windows/Fonts/msgothic.ttc",
        # macOS
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        # 数学記号の代替用
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/seguisym.ttf",
        "C:/Windows/Fonts/cambria.ttc",
    ]


# ==========================================
# 状態定義（ステートマシン）
# ==========================================
class AppState(Enum):
    IDLE = auto()          # 待機中
    MOVING = auto()        # 上下移動中
    STABILIZING = auto()   # 停止検知、静止確認中（カウントダウン）
    CAPTURING = auto()     # 4K撮影中
    COOLDOWN = auto()      # クールダウン


def _camera_backend():
    """OSに合わせてOpenCVのカメラバックエンドを選ぶ（LinuxはV4L2、WindowsはDirectShow）"""
    system = platform.system()
    if system == "Linux":
        return cv2.CAP_V4L2
    if system == "Windows":
        return cv2.CAP_DSHOW
    return cv2.CAP_ANY


# ==========================================
# 安全な動的解像度変更カメラ
# ==========================================
class SafeDynamicCamera:
    """別スレッドで常に最新フレームを読み込み、撮影時だけ高解像度に切り替えるカメラ。

    撮影（解像度の切り替え → 4K画像の取得 → 監視解像度への復帰）はすべてカメラ用スレッドで行い、
    メインスレッド（画面表示・キー入力）を止めないようにしている。
    """

    def __init__(self, src=0, width=1920, height=1080, request_fps=30):
        self.src = src
        self.monitor_size = (width, height)
        self.request_fps = request_fps

        self.stopped = False

        self.lock = threading.Lock()
        self.new_frame_event = threading.Event()

        self.ret = False
        self.frame = None

        # 撮影要求（request_capture で設定し、カメラ用スレッドが処理する）
        self.capture_event = threading.Event()
        self.capture_size = None
        self.capture_callback = None
        self.busy = False

    def start(self):
        threading.Thread(target=self._thread_loop, daemon=True).start()
        return self

    def _open(self, size):
        """指定した解像度でカメラを開く。開けないときは少し待って再試行する"""
        cap = None
        for _ in range(Config.OPEN_RETRIES):
            cap = cv2.VideoCapture(self.src, _camera_backend())
            if cap.isOpened():
                break
            cap.release()
            time.sleep(0.2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
        cap.set(cv2.CAP_PROP_FPS, self.request_fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _grab_photo(self, cap):
        """切り替え直後の不安定なフレームを捨ててから1枚取得する（最大 CAPTURE_TIMEOUT 秒待つ）"""
        deadline = time.time() + Config.CAPTURE_TIMEOUT
        frame = None
        good = 0
        while time.time() < deadline and not self.stopped:
            ret, f = cap.read()
            if ret and f is not None:
                frame = f
                good += 1
                if good > Config.CAPTURE_SKIP_FRAMES:
                    break
        return frame

    def _do_capture(self, cap):
        """監視用 → 撮影用 → 監視用 と切り替えて1枚撮る。新しい監視用の cap を返す"""
        t0 = time.time()
        cap.release()
        cap = self._open(self.capture_size)
        t1 = time.time()
        frame = self._grab_photo(cap)
        t2 = time.time()
        cap.release()
        cap = self._open(self.monitor_size)
        t3 = time.time()
        logging.info(f"撮影時間: 切替 {t1 - t0:.1f}秒 / 取得 {t2 - t1:.1f}秒 / 復帰 {t3 - t2:.1f}秒")

        callback = self.capture_callback
        self.busy = False
        if callback:
            callback(frame)
        return cap

    def _thread_loop(self):
        cap = self._open(self.monitor_size)

        while not self.stopped:
            if self.capture_event.is_set():
                self.capture_event.clear()
                cap = self._do_capture(cap)
                continue

            if not cap.isOpened():
                time.sleep(0.01)
                continue

            ret, frame = cap.read()
            if ret and frame is not None:
                with self.lock:
                    self.ret = ret
                    self.frame = frame
                self.new_frame_event.set()
            else:
                time.sleep(0.005)

        cap.release()

    def read(self, timeout=0.1):
        """新しいフレームが届くまで最大 timeout 秒待って返す（撮影中は画面を止めないよう短めに待つ）"""
        if not self.new_frame_event.wait(timeout=timeout):
            return False, None

        self.new_frame_event.clear()
        with self.lock:
            return self.ret, self.frame.copy() if self.frame is not None else None

    def request_capture(self, size, callback):
        """撮影を予約してすぐに戻る。撮影が終わるとカメラ用スレッドから callback(frame) が呼ばれる
        （失敗時は frame が None）。撮影中なら False を返す"""
        if self.busy:
            return False
        self.busy = True
        self.capture_size = size
        self.capture_callback = callback
        self.capture_event.set()
        return True

    def stop(self):
        self.stopped = True


def draw_text_with_bg(img, text, position, font_scale, color, thickness, bg_color=(0, 0, 0, 150)):
    """半透明の背景付きで文字を描画する（cv2.putTextは日本語非対応のため表示は英語）"""
    font = cv2.FONT_HERSHEY_SIMPLEX
    (text_w, text_h), baseline = cv2.getTextSize(text, font, font_scale, thickness)
    x, y = position

    overlay = img.copy()
    cv2.rectangle(overlay, (x - 5, y - text_h - 5), (x + text_w + 5, y + baseline + 5), bg_color[:3], -1)
    alpha = bg_color[3] / 255.0 if len(bg_color) == 4 else 0.6
    cv2.addWeighted(overlay, alpha, img, 1 - alpha, 0, img)

    cv2.putText(img, text, (x, y), font, font_scale, color, thickness)


# ==========================================
# 黒板の動きを検知して自動撮影するクラス
# ==========================================
class BlackboardApp:
    def __init__(self, save_dir):
        self.save_dir = save_dir
        os.makedirs(self.save_dir, exist_ok=True)

        # 撮影した画像のパス（授業終了後にGeminiへ送る）
        self.image_paths = []
        self.paths_lock = threading.Lock()
        self.photo_count = 0
        self.capture_done = threading.Event()   # カメラ用スレッドで撮影が終わったら立つ
        self.save_threads = []                  # 画像を保存中のスレッド
        self.last_display = None                # 撮影中に表示し続ける最後の画面

        self.cam = SafeDynamicCamera(Config.SRC, *Config.FHD_SIZE, Config.FPS)

        self.aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        self.aruco_params = cv2.aruco.DetectorParameters()
        # OpenCV 4.7以降は ArucoDetector、それ以前は cv2.aruco.detectMarkers を使う
        self.detector = cv2.aruco.ArucoDetector(self.aruco_dict, self.aruco_params) if hasattr(cv2.aruco, 'ArucoDetector') else None

        self.state = AppState.IDLE
        self.prev_y_ratio = None
        self.start_move_y_ratio = None
        self.stable_start_time = 0.0
        self.cooldown_start_time = 0.0
        self.fps = 0.0

    def run(self):
        """'q' またはESCが押されるまで監視・撮影を続け、撮影した画像のパス一覧を返す"""
        logging.info("システム起動: 監視用FHDモードでカメラを準備中...")
        logging.info("授業が終わったら 'q' キー（またはESC）を押してください。要約とPDF作成に進みます。")
        self.cam.start()
        time.sleep(1.0)

        prev_time = time.time()

        try:
            while True:
                self._check_capture_done()

                ret, frame = self.cam.read()
                if not ret or frame is None:
                    # 撮影で解像度を切り替えている間もウィンドウを固まらせない
                    if self.last_display is not None:
                        shown = self.last_display.copy()
                        self._draw_hud(shown)
                        cv2.imshow("Blackboard Auto-Capture System", shown)
                    if self._quit_pressed():
                        break
                    continue

                curr_time = time.time()
                self.fps = 1.0 / (curr_time - prev_time) if (curr_time - prev_time) > 0 else 0
                prev_time = curr_time

                self._process_frame(frame)

                if self._quit_pressed():
                    logging.info("終了コマンドを受信しました。")
                    break

        finally:
            # 撮影中・保存中の画像があれば終わるまで待つ
            if self.state == AppState.CAPTURING:
                logging.info("撮影中の画像を待っています...")
                self.capture_done.wait(timeout=Config.CAPTURE_TIMEOUT + 10)
            for t in self.save_threads:
                t.join()
            self.cam.stop()
            cv2.destroyAllWindows()
            logging.info(f"カメラを停止しました。撮影枚数: {len(self.image_paths)} 枚")

        return sorted(self.image_paths)

    @staticmethod
    def _quit_pressed():
        key = cv2.waitKey(1) & 0xFF
        return key in (ord('q'), 27)  # 27 = ESC

    def _process_frame(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if self.detector:
            corners, ids, _ = self.detector.detectMarkers(gray)
        else:
            corners, ids, _ = cv2.aruco.detectMarkers(gray, self.aruco_dict, parameters=self.aruco_params)

        h, w = frame.shape[:2]
        current_y_ratio = None

        # ID: TARGET_ARUCO_ID のマーカーのみを抽出して処理
        if ids is not None:
            flat_ids = ids.ravel()
            if Config.TARGET_ARUCO_ID in flat_ids:
                # 対象IDのインデックスを取得
                idx = list(flat_ids).index(Config.TARGET_ARUCO_ID)
                marker_corners = corners[idx][0]

                # マーカー中心のY座標を画面高に対する割合で求める（解像度に依存しない）
                center_y = sum([p[1] for p in marker_corners]) / 4.0
                current_y_ratio = center_y / h

                # 対象マーカーの枠と情報を描画
                pts = marker_corners.astype(int)
                cv2.polylines(frame, [pts], True, (0, 255, 0), 2)
                draw_text_with_bg(frame, f"ID:{Config.TARGET_ARUCO_ID} Y:{current_y_ratio*100:.1f}%",
                                  (pts[0][0], max(30, pts[0][1] - 10)), 0.6, (0, 255, 0), 2)

        self._update_state(frame, current_y_ratio)
        self.last_display = frame.copy()
        self._draw_hud(frame)
        cv2.imshow("Blackboard Auto-Capture System", frame)

    def _update_state(self, frame, current_y_ratio):
        """両方向検知 & 誤動作防止強化ロジック"""

        # 対象マーカーが見つからない場合は処理をスキップ
        if current_y_ratio is None:
            return

        if self.prev_y_ratio is None:
            self.prev_y_ratio = current_y_ratio
            return

        delta = abs(self.prev_y_ratio - current_y_ratio)

        # 1. 待機状態 (IDLE)
        if self.state == AppState.IDLE:
            if delta > Config.MOVE_RATIO_THRESHOLD:
                logging.info(f"ID:{Config.TARGET_ARUCO_ID} の移動を検知しました。")
                self.start_move_y_ratio = self.prev_y_ratio
                self.state = AppState.MOVING

        # 2. 移動中 (MOVING)
        elif self.state == AppState.MOVING:
            if delta <= Config.STABLE_RATIO_THRESHOLD:
                total_move = abs(self.start_move_y_ratio - current_y_ratio)

                if total_move >= Config.MIN_TOTAL_MOVE_RATIO:
                    logging.info(f"移動完了（総移動量: {total_move*100:.1f}%）。静止カウントダウン開始...")
                    self.stable_start_time = time.time()
                    self.state = AppState.STABILIZING
                else:
                    logging.info(f"移動量が小さいため無視しました（総移動量: {total_move*100:.1f}%）")
                    self.state = AppState.IDLE

        # 3. 静止カウントダウン中 (STABILIZING)
        elif self.state == AppState.STABILIZING:
            if delta > Config.STABLE_RATIO_THRESHOLD:
                logging.info("静止中に再び動作を検知。カウントダウンをリセットします。")
                self.state = AppState.MOVING
            else:
                elapsed = time.time() - self.stable_start_time
                if elapsed >= Config.STABLE_TIME:
                    self.state = AppState.CAPTURING
                    self._execute_capture(frame)

        # 4. クールダウン中 (COOLDOWN)
        elif self.state == AppState.COOLDOWN:
            elapsed = time.time() - self.cooldown_start_time
            if elapsed >= Config.COOLDOWN_TIME:
                logging.info("クールダウン終了。監視を再開します。")
                self.state = AppState.IDLE

        self.prev_y_ratio = current_y_ratio

    def _execute_capture(self, current_frame):
        """撮影を予約するだけですぐ戻る（切り替え・取得はカメラ用スレッド、保存は別スレッドで行う）"""
        logging.info("4K高画質撮影モードへ切り替え中...")
        self.capture_done.clear()
        if not self.cam.request_capture(Config.UHD_SIZE, self._on_photo):
            logging.warning("前の撮影がまだ終わっていないため、今回はスキップします。")
            self.capture_done.set()

    def _on_photo(self, frame_4k):
        """カメラ用スレッドから呼ばれる。保存は別スレッドに任せてカメラをすぐ監視に戻す"""
        if frame_4k is None:
            logging.error("4K画像のキャプチャに失敗しました")
        else:
            self.photo_count += 1
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(self.save_dir, f"board_{self.photo_count:02d}_{timestamp}.jpg")
            t = threading.Thread(target=self._save_photo, args=(filepath, frame_4k))
            t.start()
            self.save_threads.append(t)
        self.capture_done.set()

    def _save_photo(self, filepath, frame):
        if cv2.imwrite(filepath, frame, [cv2.IMWRITE_JPEG_QUALITY, Config.JPEG_QUALITY]):
            with self.paths_lock:
                self.image_paths.append(filepath)
                n = len(self.image_paths)
            logging.info(f"保存成功: {filepath}（{n} 枚目）")
        else:
            logging.error(f"画像の保存に失敗しました: {filepath}")

    def _check_capture_done(self):
        """撮影が終わっていたらクールダウンへ進む（マーカーが見えていなくても進める）"""
        if self.state == AppState.CAPTURING and self.capture_done.is_set():
            logging.info("監視モード(FHD)に復帰しました。")
            self.cooldown_start_time = time.time()
            self.state = AppState.COOLDOWN
            self.prev_y_ratio = None

    def _draw_hud(self, frame):
        state_info = {
            AppState.IDLE: (f"WAITING FOR ID:{Config.TARGET_ARUCO_ID}...", (255, 255, 255)),
            AppState.MOVING: ("BOARD MOVING...", (0, 255, 255)),
            AppState.STABILIZING: (f"STOPPED! CHECKING {(Config.STABLE_TIME - (time.time() - self.stable_start_time)):.1f}s", (0, 165, 255)),
            AppState.CAPTURING: ("CAPTURING 4K PHOTO...", (0, 0, 255)),
            AppState.COOLDOWN: (f"COOLDOWN {(Config.COOLDOWN_TIME - (time.time() - self.cooldown_start_time)):.1f}s", (0, 255, 0))
        }

        status_text, color = state_info.get(self.state, ("UNKNOWN", (200, 200, 200)))

        draw_text_with_bg(frame, f"FPS: {self.fps:.1f}", (20, 40), 0.8, (0, 255, 255), 2)
        draw_text_with_bg(frame, f"STATE: {status_text}", (20, 80), 0.8, color, 2)
        draw_text_with_bg(frame, f"IMAGES: {len(self.image_paths)}  [Q] FINISH & SUMMARIZE", (20, 120), 0.8, (255, 255, 255), 2)


# ==========================================
# LaTeX → 人が読める形式への変換
# ==========================================
class LatexToReadable:
    """
    Geminiの出力に含まれるLaTeX数式を、PDFでそのまま読めるUnicodeテキストに変換する。
      例: $\\frac{a+b}{2}$        → (a + b)/2
          $x^2 + y_1$            → x² + y₁
          $\\sqrt{x}$, $\\alpha$  → √x, α
          $\\sum_{i=1}^{n} i$    → Σᵢ₌₁ⁿ i
    $...$ / $$...$$ / \\(...\\) / \\[...\\] / \\begin{align}...\\end{align} を対象とする。
    """

    # コマンド名 → 置き換え文字（演算子・関係記号は前後に空白を入れて読みやすくする）
    SYMBOLS = {
        # ギリシャ文字（小文字）
        "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
        "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
        "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
        "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
        # ギリシャ文字（大文字）
        "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
        "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
        # 四則演算
        "times": " × ", "cdot": " · ", "div": " ÷ ", "pm": " ± ", "mp": " ∓ ", "ast": "*",
        # 関係
        "leq": " ≤ ", "le": " ≤ ", "geq": " ≥ ", "ge": " ≥ ", "neq": " ≠ ", "ne": " ≠ ",
        "approx": " ≈ ", "equiv": " ≡ ", "sim": " ~ ", "simeq": " ≃ ", "cong": " ≅ ",
        "propto": " ∝ ", "ll": " ≪ ", "gg": " ≫ ", "leqq": " ≦ ", "geqq": " ≧ ",
        # 矢印
        "to": " → ", "rightarrow": " → ", "leftarrow": " ← ", "Rightarrow": " ⇒ ",
        "Leftarrow": " ⇐ ", "leftrightarrow": " ↔ ", "Leftrightarrow": " ⇔ ",
        "iff": " ⇔ ", "implies": " ⇒ ", "mapsto": " ↦ ", "uparrow": "↑", "downarrow": "↓",
        "longrightarrow": " → ", "Longrightarrow": " ⇒ ", "rightleftharpoons": " ⇌ ",
        # 集合・論理
        "in": " ∈ ", "notin": " ∉ ", "ni": " ∋ ", "subset": " ⊂ ", "subseteq": " ⊆ ",
        "supset": " ⊃ ", "supseteq": " ⊇ ", "cup": " ∪ ", "cap": " ∩ ", "setminus": " ∖ ",
        "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃", "neg": "¬",
        "land": " ∧ ", "lor": " ∨ ", "wedge": " ∧ ", "vee": " ∨ ",
        # 解析・その他
        "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "Σ", "prod": "∏",
        "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮",
        "angle": "∠", "perp": " ⊥ ", "parallel": " ∥ ", "triangle": "△", "circ": "∘",
        "degree": "°", "prime": "′", "cdots": "⋯", "ldots": "…", "dots": "…", "vdots": "⋮",
        "ddots": "⋱", "therefore": "∴ ", "because": "∵ ", "hbar": "ℏ", "ell": "ℓ",
        # 括弧
        "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
        "lvert": "|", "rvert": "|", "vert": "|", "lVert": "‖", "rVert": "‖", "Vert": "‖", "mid": " | ",
        # 空白
        "quad": "  ", "qquad": "    ", "space": " ",
    }

    # 関数名（そのまま英字で出力し、後ろに文字が続く場合は空白を入れる）
    FUNCTIONS = {
        "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
        "sinh", "cosh", "tanh", "log", "ln", "lg", "exp", "lim", "max", "min",
        "sup", "inf", "det", "dim", "gcd", "deg", "arg", "Pr", "mod", "bmod",
    }

    # \text{} などの「中身をそのまま残す」コマンド
    TEXT_COMMANDS = [
        "text", "textrm", "textbf", "textit", "mathrm", "mathbf", "mathit", "mathsf",
        "mathcal", "mathscr", "mathfrak", "boldsymbol", "bm", "operatorname", "mbox", "emph",
    ]

    # \mathbb{R} → ℝ など
    BLACKBOARD = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ"}

    # 上付き・下付き文字に変換できる文字
    SUPERSCRIPT = dict(zip(
        "0123456789+-=()abcdefghijklmnoprstuvwxyzABDEGHIJKLMNOPRTUVW",
        "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ",
    ))
    SUBSCRIPT = dict(zip(
        "0123456789+-=()aehijklmnoprstuvx",
        "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ",
    ))

    # エスケープ文字を一時的に退避するための目印
    _PLACEHOLDERS = {r"\{": "\x01", r"\}": "\x02", r"\_": "\x03", r"\&": "\x04"}
    _RESTORE = {"\x01": "{", "\x02": "}", "\x03": "_", "\x04": "&"}

    # ------------------------------------------
    # 公開メソッド
    # ------------------------------------------
    def convert(self, text):
        """文章全体から数式部分を探し、読みやすい形に置き換える"""
        # 別行立て数式（環境・$$・\[ \]）は独立した行として字下げして出力する
        text = re.sub(r"\\begin\{(equation|align|gather|multline|eqnarray)(\*?)\}(.+?)\\end\{\1\2\}",
                      lambda m: self._display(m.group(3)), text, flags=re.S)
        text = re.sub(r"\$\$(.+?)\$\$", lambda m: self._display(m.group(1)), text, flags=re.S)
        text = re.sub(r"\\\[(.+?)\\\]", lambda m: self._display(m.group(1)), text, flags=re.S)

        # 文中の数式（\( \) と $ $）。"$5 と $10" のような金額表記は誤検出しないようにする
        text = re.sub(r"\\\((.+?)\\\)", lambda m: self._math(m.group(1)), text, flags=re.S)
        text = re.sub(r"(?<![\\$])\$(?![\s$])([^$\n]+?)(?<!\s)\$(?!\d)",
                      lambda m: self._math(m.group(1)), text)

        # $ で囲まれずに書かれた既知のコマンド（例: "A \rightarrow B"）も変換する
        text = re.sub(r"\\([a-zA-Z]+)",
                      lambda m: self.SYMBOLS[m.group(1)].strip() if m.group(1) in self.SYMBOLS else m.group(0),
                      text)
        return text.replace(r"\$", "$")

    # ------------------------------------------
    # 内部処理
    # ------------------------------------------
    def _display(self, expr):
        """別行立て数式: \\\\ で改行し、各行を字下げして独立した行にする"""
        lines = [self._math(line) for line in re.split(r"\\\\", expr)]
        lines = [line for line in lines if line]
        return "\n" + "\n".join("    " + line for line in lines) + "\n"

    def _math(self, expr):
        """数式1つ分を変換する"""
        s = expr

        # 1. エスケープされた記号を退避
        for src, ph in self._PLACEHOLDERS.items():
            s = s.replace(src, ph)
        s = s.replace(r"\%", "%").replace(r"\#", "#").replace(r"\$", "$")

        # 2. 見た目だけのコマンドを削除
        s = re.sub(r"\\(left|right)\.", "", s)
        s = re.sub(r"\\(left|right|[Bb]igg?[lr]?)(?![a-zA-Z])", "", s)
        s = re.sub(r"\\(displaystyle|textstyle|limits|nolimits)(?![a-zA-Z])", "", s)
        s = re.sub(r"\\begin\{[^}]*\}|\\end\{[^}]*\}", "", s)
        s = re.sub(r"\\[,;:>]", " ", s)
        s = s.replace(r"\!", "").replace("\\ ", " ")
        s = s.replace("\\\\", "; ").replace("&", "")

        # 3. 中身をそのまま残すコマンド（\text{...} など）
        #    \text{ if } のような文章は前後の空白も含めてそのまま残す
        s = self._apply_command(s, self.TEXT_COMMANDS, 1,
                                lambda name, opt, a: a if name in ("text", "textrm", "mbox", "emph") else self._math(a))
        s = self._apply_command(s, ["mathbb"], 1,
                                lambda name, opt, a: "".join(self.BLACKBOARD.get(c, c) for c in self._math(a)))

        # 4. 分数・二項係数・平方根
        s = self._apply_command(s, ["frac", "dfrac", "tfrac", "cfrac"], 2,
                                lambda name, opt, a, b: f"{self._wrap(self._math(a))}/{self._wrap(self._math(b))}")
        s = self._apply_command(s, ["binom", "dbinom", "tbinom"], 2,
                                lambda name, opt, a, b: f"C({self._math(a)}, {self._math(b)})")
        s = self._apply_command(s, ["sqrt"], 1, self._sqrt, optional=True)

        # 5. 文字の上に付ける記号（ベクトル・ハット・バーなど）
        s = self._apply_command(s, ["vec", "overrightarrow"], 1, lambda name, opt, a: self._math(a) + "\u20d7")
        s = self._apply_command(s, ["hat", "widehat"], 1, lambda name, opt, a: self._math(a) + "\u0302")
        s = self._apply_command(s, ["tilde", "widetilde"], 1, lambda name, opt, a: self._math(a) + "\u0303")
        s = self._apply_command(s, ["dot"], 1, lambda name, opt, a: self._math(a) + "\u0307")
        s = self._apply_command(s, ["ddot"], 1, lambda name, opt, a: self._math(a) + "\u0308")
        s = self._apply_command(s, ["bar", "overline"], 1,
                                lambda name, opt, a: "".join(c + "\u0305" for c in self._math(a)))
        s = self._apply_command(s, ["underline"], 1, lambda name, opt, a: self._math(a))

        # 6. 角度の「度」（90^\circ → 90°）
        s = re.sub(r"\^\s*\{?\s*\\circ\s*\}?", "°", s)

        # 7. 記号・ギリシャ文字・関数名などのコマンドを置換
        s = re.sub(r"\\([a-zA-Z]+)", self._replace_symbol, s)

        # 8. 上付き・下付き文字
        s = self._scripts(s)

        # 9. 残った波括弧を外し、退避した記号を戻し、空白を整える
        s = s.replace("{", "").replace("}", "")
        for ph, ch in self._RESTORE.items():
            s = s.replace(ph, ch)
        s = re.sub(r"[ \t]{2,}", " ", s)
        s = re.sub(r"\(\s+", "(", s)
        s = re.sub(r"\s+\)", ")", s)
        return s.strip()

    def _replace_symbol(self, m):
        name = m.group(1)
        if name in self.SYMBOLS:
            return self.SYMBOLS[name]
        if name in self.FUNCTIONS:
            # \sin\theta → "sin θ"、\sin(x) → "sin(x)"
            nxt = m.string[m.end():m.end() + 1]
            return name + " " if (nxt.isalnum() or nxt == "\\") else name
        # 未知のコマンドはバックスラッシュだけ外して残す
        return name

    def _sqrt(self, name, opt, a):
        inner = self._wrap(self._math(a))
        if opt:
            index = self._math(opt)
            sup = self._to_script(index, self.SUPERSCRIPT)
            return (sup if sup else f"({index})") + "√" + inner
        return "√" + inner

    @staticmethod
    def _wrap(x):
        """1語（数字・文字のみ）ならそのまま、式なら括弧で囲む"""
        if re.fullmatch(r"[^\s+\-−×·÷/=<>≤≥±∓,;]+", x) or (x.startswith("(") and x.endswith(")")):
            return x
        return f"({x})"

    @staticmethod
    def _read_group(s, i):
        """位置 i から {…} または1文字（1コマンド）を読み、(中身, 次の位置) を返す"""
        while i < len(s) and s[i] in " \t\n":
            i += 1
        if i >= len(s):
            return "", i
        if s[i] == "{":
            depth = 0
            for j in range(i, len(s)):
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                    if depth == 0:
                        return s[i + 1:j], j + 1
            return s[i + 1:], len(s)  # 閉じ括弧がない場合は最後までを中身とする
        if s[i] == "\\":
            m = re.match(r"\\([a-zA-Z]+|.)", s[i:])
            return m.group(0), i + len(m.group(0))
        return s[i], i + 1

    def _apply_command(self, s, names, nargs, fn, optional=False):
        """\\name[opt]{arg1}{arg2}... を探し、fn(name, opt, *args) の結果に置き換える"""
        pattern = re.compile(r"\\(" + "|".join(names) + r")(?![a-zA-Z])")
        while True:
            m = pattern.search(s)
            if not m:
                return s
            i = m.end()
            opt = None
            if optional:
                j = i
                while j < len(s) and s[j] == " ":
                    j += 1
                if j < len(s) and s[j] == "[":
                    close = s.find("]", j)
                    if close != -1:
                        opt, i = s[j + 1:close], close + 1
            args = []
            for _ in range(nargs):
                arg, i = self._read_group(s, i)
                args.append(arg)
            s = s[:m.start()] + fn(m.group(1), opt, *args) + s[i:]

    def _scripts(self, s):
        """^ と _ を Unicode の上付き・下付き文字に変換する（変換できない場合は ^(…) 表記）"""
        out = []
        i = 0
        while i < len(s):
            c = s[i]
            if c in "^_":
                content, i = self._read_group(s, i + 1)
                content = self._scripts(content).replace("{", "").replace("}", "").strip()
                if c == "^" and content in ("∘", "°"):
                    out.append("°")
                    continue
                if c == "^" and content and set(content) <= {"′"}:
                    out.append(content)
                    continue
                table = self.SUPERSCRIPT if c == "^" else self.SUBSCRIPT
                converted = self._to_script(content, table)
                if converted:
                    out.append(converted)
                elif len(content) == 1:
                    out.append(c + content)
                else:
                    out.append(f"{c}({content})")
            else:
                out.append(c)
                i += 1
        return "".join(out)

    @staticmethod
    def _to_script(content, table):
        """全ての文字が上付き（下付き）に変換できる場合のみ変換結果を返す"""
        content = content.replace(" ", "")
        if content and all(ch in table for ch in content):
            return "".join(table[ch] for ch in content)
        return None


# ==========================================
# Gemini による要約
# ==========================================
def summarize_with_gemini(image_paths):
    """撮影した画像をまとめてGeminiに送り、要約テキストを返す（失敗時は None）"""
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        logging.error("環境変数 GEMINI_API_KEY が設定されていません。APIキーを設定してから再実行してください。")
        return None

    # プロンプト（テキスト）と全画像を1つのリストにまとめて、1回の呼び出しで送る
    contents = [Config.SUMMARY_PROMPT]
    for img_path in image_paths:
        contents.append(PIL.Image.open(img_path))

    logging.info(f"{len(image_paths)} 枚の画像をGeminiに送信して要約中...")
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=Config.GEMINI_MODEL,
            contents=contents,
        )
        if not response.text:
            logging.error("AI処理エラー: Geminiから空の応答が返されました。")
            return None
        return response.text
    except Exception as e:
        logging.error(f"AI処理エラー: {e}")
        return None


# ==========================================
# PDF レポート生成
# ==========================================
def _inline_markdown(text):
    """**太字**、`コード`、[リンク](URL) などの記号を外して普通の文字にする"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    return text


def markdown_to_blocks(text):
    """Markdownを (種類, 文字列, 字下げ段数) のリストに変換する"""
    blocks = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            blocks.append(("blank", "", 0))
            continue

        indent = min((len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())) // 2, 6)

        # 水平線（--- や ***）
        if re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", line):
            blocks.append(("rule", "", 0))
            continue

        # 見出し（# / ## / ###...）
        m = re.match(r"^\s*(#{1,6})\s+(.*)", line)
        if m:
            style = {1: "h1", 2: "h2"}.get(len(m.group(1)), "h3")
            blocks.append((style, _inline_markdown(m.group(2)).strip(), 0))
            continue

        # 箇条書き（* / - / +）
        m = re.match(r"^\s*[-*+]\s+(.*)", line)
        if m:
            blocks.append(("text", "• " + _inline_markdown(m.group(1)), indent))
            continue

        blocks.append(("text", _inline_markdown(line.strip()), indent))
    return blocks


def _setup_unicode_fonts(pdf):
    """Unicodeフォントを登録してフォント名を返す。1つも見つからなければ None"""
    loaded = []
    for path in Config.FONT_CANDIDATES:
        if not path or not os.path.exists(path):
            continue
        name = f"UniFont{len(loaded)}"
        try:
            pdf.add_font(name, "", path)
            loaded.append(name)
        except Exception as e:
            logging.warning(f"フォントを読み込めませんでした（{path}）: {e}")

    if not loaded:
        return None

    # メインフォントに無い文字（数学記号など）は代替フォントで表示する
    if len(loaded) > 1 and hasattr(pdf, "set_fallback_fonts"):
        pdf.set_fallback_fonts(loaded[1:])
    return loaded[0]


def create_pdf_report(text, pdf_path, title):
    """読みやすく変換済みのテキストからPDFを作成する"""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    family = _setup_unicode_fonts(pdf)
    if family is None:
        # Unicodeフォントが無い場合は標準フォントで出力（日本語や記号は '?' になる）
        logging.warning("日本語フォントが見つかりません。PDFの日本語・数式記号が「?」になります。"
                        "次のコマンドでインストールしてください: sudo apt install fonts-noto-cjk fonts-dejavu-core")
        family = "Helvetica"

    def safe(t):
        if family != "Helvetica":
            return t
        return t.replace("•", "-").encode("latin-1", "replace").decode("latin-1")

    sizes = {"title": 18, "h1": 16, "h2": 14, "h3": 12, "text": 11}

    # タイトル
    pdf.set_font(family, size=sizes["title"])
    pdf.multi_cell(0, sizes["title"] * 0.55, safe(title), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    for style, line, indent in markdown_to_blocks(text):
        if style == "blank":
            pdf.ln(2)
            continue
        if style == "rule":
            y = pdf.get_y() + 1
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(3)
            continue
        if style in ("h1", "h2", "h3"):
            pdf.ln(2)

        size = sizes[style]
        pdf.set_font(family, size=size)
        pdf.set_x(pdf.l_margin + indent * 5)
        pdf.multi_cell(0, size * 0.55, safe(line), new_x="LMARGIN", new_y="NEXT")

    pdf.output(pdf_path)


# ==========================================
# メイン処理
# ==========================================
def main():
    # 1. 授業ごとのフォルダを作成
    session_name = datetime.now().strftime("Class_%Y%m%d_%H%M")
    session_dir = os.path.join(Config.BASE_SAVE_DIR, session_name)
    os.makedirs(session_dir, exist_ok=True)
    logging.info(f"授業セッション開始！保存先: {session_dir}")

    # 2. 黒板の動きを検知して自動撮影（'q' で終了）
    app = BlackboardApp(session_dir)
    image_paths = app.run()

    if not image_paths:
        logging.info("画像が撮影されなかったため、要約を行わずに終了します。")
        return

    # 3. Geminiで要約
    ai_text = summarize_with_gemini(image_paths)
    if ai_text is None:
        logging.info(f"要約できませんでしたが、撮影した画像は {session_dir} に保存されています。")
        return
    logging.info("AI解析完了！PDFを作成中...")

    # 4. 生の出力を保存し、LaTeXを読みやすい形に変換したものも保存
    with open(os.path.join(session_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(ai_text)

    readable_text = LatexToReadable().convert(ai_text)
    with open(os.path.join(session_dir, "summary_readable.txt"), "w", encoding="utf-8") as f:
        f.write(readable_text)

    # 5. PDFレポートを作成
    pdf_path = os.path.join(session_dir, "Summary_Report.pdf")
    try:
        create_pdf_report(readable_text, pdf_path, f"授業まとめ - {session_name}")
        logging.info(f"完了！要約レポートを保存しました: {pdf_path}")
    except Exception as e:
        logging.error(f"PDF作成エラー: {e}")


if __name__ == "__main__":
    main()
