import os
import cv2
import time
import threading
import logging
import platform
from enum import Enum, auto

# Qt系の不要なログ出力を抑制
os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.qpa.*=false"

# ログの設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ==========================================
# 設定パラメータ（現場に合わせて調整）
# ==========================================
class Config:
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
    KEEP_HIGH_RES = False          # True: 最初から撮影用の解像度で動かし、切り替えをしない
                                   #       （切り替えで timeout が続く場合に。CPU負荷が増えFPSは下がる）
    CAPTURE_TIMEOUT = 5.0          # 高解像度の画像が届くまで待つ最大時間（秒）
    CAPTURE_SKIP_FRAMES = 2        # 切り替え直後に捨てるフレーム数（露出が安定するまで）
    CAPTURE_ATTEMPTS = 2           # 高解像度で撮れなかったときに開き直して試す回数
    SWITCH_DELAY = 0.5             # カメラを閉じてから開き直すまでの待ち時間（秒）
    OPEN_RETRIES = 5               # カメラを開くときの最大試行回数
    STALL_TIMEOUT = 3.0            # この秒数映像が届かなければカメラを自動で開き直す
    JPEG_QUALITY = 95              # 保存するJPEGの画質（0〜100）

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
    Config.KEEP_HIGH_RES = True のときは最初から撮影用の解像度で開き、切り替えを一切しない。
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
        for attempt in range(1, Config.OPEN_RETRIES + 1):
            cap = cv2.VideoCapture(self.src, _camera_backend())
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
                cap.set(cv2.CAP_PROP_FPS, self.request_fps)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                return cap
            cap.release()
            logging.warning(f"カメラを開けませんでした。再試行します（{attempt}/{Config.OPEN_RETRIES}）")
            time.sleep(0.5)
        logging.error("カメラを開けませんでした。USBの接続と Config.SRC を確認してください。")
        return cap

    def _reopen(self, cap, size):
        """カメラを閉じ、USB・ドライバが落ち着くまで待ってから指定の解像度で開き直す"""
        cap.release()
        time.sleep(Config.SWITCH_DELAY)
        return self._open(size)

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
        """撮影用の解像度で1枚撮り、監視用に戻す。新しい cap を返す"""
        t0 = time.time()
        frame = None

        if Config.KEEP_HIGH_RES:
            # すでに撮影用の解像度で動いているので、次のフレームを取るだけ
            frame = self._grab_photo(cap)
            logging.info(f"撮影時間: {time.time() - t0:.1f}秒（切り替えなし）")
        else:
            for attempt in range(1, Config.CAPTURE_ATTEMPTS + 1):
                cap = self._reopen(cap, self.capture_size)
                frame = self._grab_photo(cap)
                if frame is not None:
                    break
                logging.warning(f"高解像度の画像が届きませんでした（{attempt}/{Config.CAPTURE_ATTEMPTS}回目）")
            t1 = time.time()
            cap = self._reopen(cap, self.monitor_size)
            logging.info(f"撮影時間: 高解像度で取得 {t1 - t0:.1f}秒 / 監視に復帰 {time.time() - t1:.1f}秒")

        if frame is None:
            # 高解像度で撮れなかった場合は、撮影直前の監視用の画像で代用する（何も残らないよりよい）
            with self.lock:
                fallback = self.frame.copy() if self.frame is not None else None
            if fallback is not None:
                logging.warning("高解像度で撮影できなかったため、撮影直前の監視用画像（低解像度）を保存します。")
                frame = fallback

        callback = self.capture_callback
        self.busy = False
        if callback:
            callback(frame)
        return cap

    def _thread_loop(self):
        cap = self._open(self.monitor_size)
        last_ok = time.time()

        while not self.stopped:
            if self.capture_event.is_set():
                self.capture_event.clear()
                cap = self._do_capture(cap)
                last_ok = time.time()
                continue

            ret, frame = cap.read() if cap.isOpened() else (False, None)
            if ret and frame is not None:
                last_ok = time.time()
                with self.lock:
                    self.ret = ret
                    self.frame = frame
                self.new_frame_event.set()
            elif time.time() - last_ok > Config.STALL_TIMEOUT:
                # 映像が止まったまま（select() timeout など）のときは自動で開き直す
                logging.warning("カメラから映像が届かないため、カメラを開き直します。")
                cap = self._reopen(cap, self.monitor_size)
                last_ok = time.time()
            else:
                time.sleep(0.01)

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
    font = cv2.FONT_HERSHEY_SIMPLEX
    (text_w, text_h), baseline = cv2.getTextSize(text, font, font_scale, thickness)
    x, y = position
    
    overlay = img.copy()
    cv2.rectangle(overlay, (x - 5, y - text_h - 5), (x + text_w + 5, y + baseline + 5), bg_color[:3], -1)
    alpha = bg_color[3] / 255.0 if len(bg_color) == 4 else 0.6
    cv2.addWeighted(overlay, alpha, img, 1 - alpha, 0, img)
    
    cv2.putText(img, text, (x, y), font, font_scale, color, thickness)


# ==========================================
# メインアプリケーションクラス
# ==========================================
class BlackboardApp:
    def __init__(self):
        self.save_dir = os.path.expanduser("~/blackboard_images")
        os.makedirs(self.save_dir, exist_ok=True)
        
        first_size = Config.UHD_SIZE if Config.KEEP_HIGH_RES else Config.FHD_SIZE
        self.cam = SafeDynamicCamera(Config.SRC, *first_size, Config.FPS)
        self.capture_done = threading.Event()   # カメラ用スレッドで撮影が終わったら立つ
        self.save_threads = []                  # 画像を保存中のスレッド
        self.last_display = None                # 撮影中に表示し続ける最後の画面
        
        self.aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        self.aruco_params = cv2.aruco.DetectorParameters()
        self.detector = cv2.aruco.ArucoDetector(self.aruco_dict, self.aruco_params) if hasattr(cv2.aruco, 'ArucoDetector') else None

        self.state = AppState.IDLE
        self.prev_y_ratio = None
        self.start_move_y_ratio = None
        self.stable_start_time = 0.0
        self.cooldown_start_time = 0.0
        self.fps = 0.0

    def run(self):
        logging.info("システム起動: 監視用FHDモードでカメラを準備中...")
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
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        break
                    continue

                curr_time = time.time()
                self.fps = 1.0 / (curr_time - prev_time) if (curr_time - prev_time) > 0 else 0
                prev_time = curr_time

                self._process_frame(frame)
                
                if cv2.waitKey(1) & 0xFF == ord('q'):
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
            logging.info("システムを安全に終了しました。")

    def _process_frame(self, frame):
        # 高解像度のまま動かしている場合は、検出・表示用に監視サイズへ縮小する
        if frame.shape[1] > Config.FHD_SIZE[0]:
            frame = cv2.resize(frame, Config.FHD_SIZE, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        if self.detector:
            corners, ids, _ = self.detector.detectMarkers(gray)
        else:
            corners, ids, _ = cv2.aruco.detectMarkers(gray, self.aruco_dict, parameters=self.aruco_params)

        h, w = frame.shape[:2]
        current_y_ratio = None

        # ID: 0 のマーカーのみを抽出して処理
        if ids is not None:
            flat_ids = ids.ravel()
            if Config.TARGET_ARUCO_ID in flat_ids:
                # 対象IDのインデックスを取得
                idx = list(flat_ids).index(Config.TARGET_ARUCO_ID)
                marker_corners = corners[idx][0]
                
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
        
        # ID: 0 が見つからない場合は処理をスキップ
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
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(self.save_dir, f"blackboard_4k_{timestamp}.jpg")
            t = threading.Thread(target=self._save_photo, args=(filepath, frame_4k))
            t.start()
            self.save_threads.append(t)
        self.capture_done.set()

    @staticmethod
    def _save_photo(filepath, frame):
        if cv2.imwrite(filepath, frame, [cv2.IMWRITE_JPEG_QUALITY, Config.JPEG_QUALITY]):
            logging.info(f"保存成功: {filepath}")
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


if __name__ == "__main__":
    app = BlackboardApp()
    app.run()
