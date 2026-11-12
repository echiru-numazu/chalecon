import os
import cv2
import time
import threading
import logging
from enum import Enum, auto

# Qt系の不要なログ出力を抑制
os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.qpa.*=false"

# ログの設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ==========================================
# 設定パラメータ（現場に合わせて調整）
# ==========================================
class Config:
    SRC = "/dev/v4l/by-id/usb-046d_Logicool_BRIO_F097DC64-video-index0"  # Logicool BRIO webcam (fixed name; laptop camera = 0)
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

# ==========================================
# 状態定義（ステートマシン）
# ==========================================
class AppState(Enum):
    IDLE = auto()          # 待機中
    MOVING = auto()        # 上下移動中
    STABILIZING = auto()   # 停止検知、静止確認中（カウントダウン）
    CAPTURING = auto()     # 4K撮影中
    COOLDOWN = auto()      # クールダウン

# ==========================================
# 安全な動的解像度変更カメラ
# ==========================================
class SafeDynamicCamera:
    def __init__(self, src=0, width=1920, height=1080, request_fps=30):
        self.src = src
        self.target_size = (width, height)
        self.request_fps = request_fps
        
        self.stopped = False
        self.switch_requested = False
        
        self.lock = threading.Lock()
        self.new_frame_event = threading.Event()
        
        self.ret = False
        self.frame = None

    def start(self):
        threading.Thread(target=self._thread_loop, daemon=True).start()
        return self

    def _init_camera(self):
        cap = cv2.VideoCapture(self.src, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.target_size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.target_size[1])
        cap.set(cv2.CAP_PROP_FPS, self.request_fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _thread_loop(self):
        cap = self._init_camera()
        
        while not self.stopped:
            if self.switch_requested:
                cap.release()
                time.sleep(0.3)
                cap = self._init_camera()
                
                for _ in range(3):
                    cap.read()
                    time.sleep(0.05)
                    
                self.switch_requested = False
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

    def read(self):
        if not self.new_frame_event.wait(timeout=1.0):
            return False, None
        
        self.new_frame_event.clear()
        with self.lock:
            return self.ret, self.frame.copy() if self.frame is not None else None

    def change_resolution_sync(self, width, height):
        self.target_size = (width, height)
        self.switch_requested = True
        
        while self.switch_requested and not self.stopped:
            time.sleep(0.05)
        self.new_frame_event.clear()

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
        
        self.cam = SafeDynamicCamera(Config.SRC, *Config.FHD_SIZE, Config.FPS)
        
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
                ret, frame = self.cam.read()
                if not ret or frame is None:
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
            self.cam.stop()
            cv2.destroyAllWindows()
            logging.info("システムを安全に終了しました。")

    def _process_frame(self, frame):
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
        logging.info("4K高画質撮影モードへ安全に切り替え中...")
        
        self.cam.change_resolution_sync(*Config.UHD_SIZE)
        
        ret_4k, frame_4k = self.cam.read()
        if ret_4k and frame_4k is not None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(self.save_dir, f"blackboard_4k_{timestamp}.jpg")
            cv2.imwrite(filepath, frame_4k)
            logging.info(f"保存成功: {filepath}")
        else:
            logging.error("4K画像のキャプチャに失敗しました")
            
        logging.info("監視モード(FHD)へ復帰中...")
        self.cam.change_resolution_sync(*Config.FHD_SIZE)
        
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
