# Blackboard Auto-Capture + Gemini Summary — Setup & Usage Guide

## Files

| File | What it does |
|---|---|
| `blackboard_genai.py` | **Main program.** Watches the board with a USB webcam, takes a 4K photo automatically when the board slides and stops. Press `q` to send all photos to Gemini and make a PDF summary. |
| `blackboard_camera.py` | Camera-only version: takes the photos, no summary. Good for testing. |
| `marker_id0.png` | The ArUco marker to stick on the board. |

---

## 1. Setup

### Python environment (one time)

```bash
sudo apt install python3-venv          # on Ubuntu with Python 3.14: python3.14-venv
python3 -m venv myenv
source myenv/bin/activate
pip install --upgrade pip
pip install opencv-contrib-python fpdf2 google-genai pillow
```

- Use **`opencv-contrib-python`**, not `opencv-python`. ArUco marker detection is only in the contrib build.
- Use **`fpdf2`**, not the old `fpdf` package.
- Run `source myenv/bin/activate` every time you open a new terminal.

### Fonts for the PDF

Japanese text and math symbols need Unicode fonts:

```bash
sudo apt install fonts-noto-cjk fonts-dejavu-core
```

To use a specific font instead, set `PDF_FONT_PATH=/path/to/font.ttf`.

### Gemini API key

1. Go to **aistudio.google.com**, click **Get API key**, then **Create API key**.
2. Set it as an environment variable. **Never write the key in the code.**

```bash
export GEMINI_API_KEY="paste-your-key-here"                         # this terminal only
echo 'export GEMINI_API_KEY="paste-your-key-here"' >> ~/.bashrc     # permanent
source ~/.bashrc                                                    # load it now
```

Check it with `echo $GEMINI_API_KEY`. It should print your key, not a blank line.

**You don't edit any code for the key.** `summarize_with_gemini()` in `blackboard_genai.py` reads `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) from the environment and passes it to `genai.Client(api_key=...)`. Set the key in the same terminal (or in `~/.bashrc`) that you run the program from.

To change or remove the key later, edit the `export GEMINI_API_KEY=...` line in `~/.bashrc`, then open a new terminal.

If a key was ever written in code and committed or pushed to git, treat it as leaked. Delete it in AI Studio and create a new one. Deleting the file doesn't remove the key from git history.

### Gemini model

Set `GEMINI_MODEL` in `Config` in `blackboard_genai.py`:

```python
GEMINI_MODEL = "gemini-3.1-flash-lite"
```

Use a model name listed in AI Studio. A `404 … model not found` error means the name is wrong or the model was retired.

---

## 2. Camera

### Checking your cameras

```bash
# List cameras by name
for d in /sys/class/video4linux/video*; do echo "$(basename $d): $(cat $d/name)"; done

# Fixed device names (don't change after reboot or replug)
ls -l /dev/v4l/by-id/

# Optional: details and supported resolutions
sudo apt install v4l-utils
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video4 --list-formats-ext    # look for 3840x2160
```

### Choosing the camera

Set `SRC` in `Config` in both `blackboard_genai.py` and `blackboard_camera.py`:

```python
SRC = "/dev/v4l/by-id/usb-046d_Logicool_BRIO_F097DC64-video-index0"   # Logicool BRIO webcam
SRC = 0                                                               # laptop camera
```

Use the `/dev/v4l/by-id/...` name instead of a number. Numbers like `/dev/video4` are handed out in detection order and can change after a reboot or replug. The by-id name is built from the camera's serial number, so it stays the same.

### What `index` means

One USB camera usually creates several `/dev/video` devices. `index` numbers them within that camera:

| Ends with | Usually carries |
|---|---|
| `video-index0` | **The normal color picture**. Use this one. |
| `video-index1` | Metadata (no picture) |
| `video-index2` | Infrared camera (face login), if the camera has one |
| `video-index3` | Infrared metadata |

To check: `v4l2-ctl -d /dev/videoN --all | grep -A3 "Device Caps"`. A device that shows a picture lists **Video Capture**.

### Quick camera test

```bash
python -c "import cv2; c=cv2.VideoCapture('/dev/v4l/by-id/usb-046d_Logicool_BRIO_F097DC64-video-index0'); print('OK' if c.read()[0] else 'FAILED')"
```

Plug the webcam into a **USB 3 port** (often blue). 4K may not work over USB 2 or through a hub.

---

## 3. The ArUco marker

- **Use exactly one marker**: dictionary **4x4_50**, **ID 0** (`marker_id0.png`).
- **Don't use two copies of ID 0.** The tracked position jumps between them and causes false photos or countdown resets.
- Print it about **10–15 cm** wide (15–20 cm in a real classroom). Keep the **white border** and print on matte paper, not glossy.
- Stick it on the **sliding board panel itself**, not the wall or frame. The camera must see it at both the top and bottom positions.
- When the marker is detected, a **green box** with `ID:0 Y:xx.x%` appears around it.
- If the board has two panels sliding in opposite directions, put the marker on one panel. Moving only the other panel won't trigger a photo.

To create the marker again:

```bash
python -c "import cv2; m=cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), 0, 400); m=cv2.copyMakeBorder(m,50,50,50,50,cv2.BORDER_CONSTANT,value=255); cv2.imwrite('marker_id0.png', m)"
```

---

## 4. When a photo is taken

Only the marker's **up-and-down** position counts. Position is measured as a percentage of the screen height. Sideways movement is ignored.

| Step | STATE shown | Condition |
|---|---|---|
| 1. Waiting | `WAITING FOR ID:0...` | Marker is visible |
| 2. Moving | `BOARD MOVING...` | Marker moves **more than 3%** between two frames in a row |
| 3. Stopped | `STOPPED! CHECKING 2.0s` | First frame with **1.5% or less** movement, and the total move from the start is **7% or more**. Less than 7% is ignored. |
| 4. Capture | `CAPTURING 4K PHOTO...` | Holds still (1.5% or less per frame) for **2 seconds**. Any bigger movement goes back to step 2. |
| 5. Cooldown | `COOLDOWN 5.0s` | Waits 5 seconds, then goes back to waiting |

**Notes**
- Movement is compared **frame to frame**, so the board must move **fast**: at about 30 FPS, roughly a full screen height per second to start. A slow slide may never trigger a photo, or may be cut short and ignored. A lower FPS makes slower movement work.
- If the marker leaves the view, everything pauses (including the countdown and cooldown) until it comes back.
- If the board moves up and then back to the same spot, the total move is about 0, so no photo is taken.

### Tuning (`Config` at the top of each file)

| Setting | Default | Meaning |
|---|---|---|
| `MOVE_RATIO_THRESHOLD` | `0.03` | Jump per frame needed to start moving |
| `MIN_TOTAL_MOVE_RATIO` | `0.07` | Minimum total move |
| `STABLE_RATIO_THRESHOLD` | `0.015` | Most it can move per frame and still count as still |
| `STABLE_TIME` | `2.0` | Seconds it must hold still |
| `COOLDOWN_TIME` | `5.0` | Seconds to wait after a photo |
| `TARGET_ARUCO_ID` | `0` | Marker ID to follow |

---

## 5. Testing the capture (`blackboard_camera.py`)

```bash
source myenv/bin/activate
python blackboard_camera.py
```

1. Hold the printed marker (or show it on a phone) in front of the webcam.
2. Move it **quickly** up or down, then hold it still for 2 seconds.
3. The terminal shows `保存成功: ~/blackboard_images/blackboard_4k_....jpg` (save successful).
4. Press **`q`** in the camera window to quit.

Check that the photo is 4K (should print `(2160, 3840, 3)`):

```bash
python -c "import cv2,glob,os; f=max(glob.glob(os.path.expanduser('~/blackboard_images/*.jpg')),key=os.path.getmtime); print(f, cv2.imread(f).shape)"
```

| Problem | Likely cause |
|---|---|
| Stays on `WAITING` while moving | Marker not detected: use more light, a bigger marker, keep the white border, check it's ID 0 from 4x4_50 |
| Log: movement too small (`移動量が小さいため無視しました`) | Moved less than 7%. Move it farther. |
| Countdown keeps resetting (`カウントダウンをリセットします`) | Hand shaking. Rest the paper against something. |
| Log: 4K capture failed (`4K画像のキャプチャに失敗しました`) | Camera didn't send a 4K frame in time. See below. |
| `select() timeout` in the terminal | Camera opened but sends no frames. See below. |
| Camera doesn't open | Webcam not plugged in. Check `ls /dev/v4l/by-id/`. |

### Fixing `select() timeout` and "4K capture failed"

Both errors mean the camera is open but isn't sending pictures. They often show up together. When taking a photo, the program closes the camera, reopens it at 3840x2160, and waits only **1 second** for a frame (`_execute_capture` in `blackboard_genai.py`). If no frame arrives in time, it logs `4K画像のキャプチャに失敗しました`.

Try these in order:

**1. Close anything else using the camera.** That includes a browser tab, Zoom, Cheese, or an earlier run of the script (Ctrl+Z pauses a program without closing it).

```bash
sudo fuser -v /dev/video*       # which processes have the camera open
pkill -f blackboard_            # kill leftover runs of the scripts
```

**2. Check the USB speed.** This is the most common cause.

```bash
lsusb -t
```

Find the line with `Driver=uvcvideo`. **480M** means USB 2, which can't carry 4K. Move the camera to a blue USB 3 port, not through a hub, until it shows **5000M**.

**3. Unplug and replug the camera, or reload the driver.**

```bash
sudo modprobe -r uvcvideo && sudo modprobe uvcvideo
```

**4. Test the camera without Python.**

```bash
DEV=/dev/v4l/by-id/usb-046d_Logicool_BRIO_F097DC64-video-index0
v4l2-ctl -d $DEV --list-formats-ext | grep -A2 MJPG    # should list 1920x1080 and 3840x2160
v4l2-ctl -d $DEV --set-fmt-video=width=1920,height=1080,pixelformat=MJPG --stream-mmap --stream-count=60
v4l2-ctl -d $DEV --set-fmt-video=width=3840,height=2160,pixelformat=MJPG --stream-mmap --stream-count=30
```

| Result | Meaning |
|---|---|
| No `3840x2160` in the list | This camera can't do 4K, or `SRC` points to the wrong camera |
| Streams (prints `<<<<` and an fps number) | Camera is fine. It's just slower than 1 second to start at 4K, so the code needs to wait longer (not done yet). |
| Hangs or times out | USB, power or driver problem. Go to step 5. |

**5. Turn on the driver's bandwidth fix.** This often helps on laptops and Raspberry Pis.

```bash
sudo modprobe -r uvcvideo && sudo modprobe uvcvideo quirks=128
```

If that fixes it, make it permanent:

```bash
echo "options uvcvideo quirks=128" | sudo tee /etc/modprobe.d/uvcvideo.conf
```

**6. Check the device name.** `SRC` in `Config` must end in **`video-index0`**. `index1` is metadata and never sends a picture.

**7. On a Raspberry Pi, check the power.** Use the official power supply. If `vcgencmd get_throttled` prints anything other than `0x0`, the Pi is short on power and the webcam can drop out.

**If you can't get USB 3 working,** lower the photo resolution in `Config` in both files:

```python
UHD_SIZE = (2560, 1440)   # only if this size appears in --list-formats-ext
UHD_SIZE = (1920, 1080)   # same as the monitoring size, so no switching at all
```

**When it happens helps narrow it down.** If the error appears only when a photo is taken (the switch to 4K), look at steps 2, 4 and 5. If it appears right at startup, look at steps 1, 3 and 6.

---

## 6. Using with Gemini (`blackboard_genai.py`)

```bash
cd ~/chalecon
source myenv/bin/activate
export GEMINI_API_KEY="..."      # if it isn't in ~/.bashrc
python blackboard_genai.py
```

1. Photos are taken automatically, the same way as in `blackboard_camera.py`. The top-left shows `IMAGES: n  [Q] FINISH & SUMMARIZE`.
2. When class ends, press **`q`** (or ESC) in the camera window.
3. All photos are sent to Gemini (about 10–60 s), then the PDF is created.

Terminal messages:

```
カメラを停止しました。撮影枚数: 3 枚        ← camera stopped, 3 photos
3 枚の画像をGeminiに送信して要約中...      ← sending to Gemini
AI解析完了！PDFを作成中...                 ← AI done, making PDF
完了！要約レポートを保存しました: ...pdf    ← finished
```

### Output

```
~/blackboard_images/Class_YYYYMMDD_HHMM/
  ├─ board_1_....jpg ...       4K photos
  ├─ summary.txt               Gemini's raw answer
  ├─ summary_readable.txt      formulas made readable (\frac{a}{b} → a/b)
  └─ Summary_Report.pdf        final report
```

Open the newest report:

```bash
xdg-open "$(ls -d ~/blackboard_images/Class_* | tail -1)/Summary_Report.pdf"
```

### Settings

- `SUMMARY_PROMPT`: instructions sent to Gemini. They're in English, so the summary comes back in English. Add "Answer in Japanese." for a Japanese summary.
- `GEMINI_MODEL`: which Gemini model to use.

### Troubleshooting

| Message | Fix |
|---|---|
| API key not set (`環境変数 GEMINI_API_KEY が設定されていません`) | Set the key in the same terminal you run the program from |
| No photos taken (`画像が撮影されなかったため…`) | Nothing to summarize, so it just exits |
| `AI処理エラー: 404 … model not found` | Change `GEMINI_MODEL` to a current model from AI Studio |
| `AI処理エラー: 429 …` | Free usage limit reached. Wait and retry. |
| `AI処理エラー: 400 API key not valid` | Key copied wrong or deleted |
| PDF error (`PDF作成エラー`) | The text is still saved in `summary.txt` |

If the AI step fails, the photos are **not lost**. They stay in the class folder.

The code uses Google's current `google-genai` library (`from google import genai`). Don't install the old `google-generativeai` package; it is no longer maintained.

---

## 7. Running on a Raspberry Pi

The code works on a Raspberry Pi. It already uses the Linux camera system (V4L2), and the webcam's by-id name should be the same on the Pi (check with `ls /dev/v4l/by-id/`).

```bash
sudo apt update
sudo apt install python3-venv fonts-noto-cjk fonts-dejavu-core
python3 -m venv myenv
source myenv/bin/activate
pip install opencv-contrib-python fpdf2 google-genai pillow
export GEMINI_API_KEY="..."
```

| Topic | What to know |
|---|---|
| Model | Pi 4 or Pi 5 with **64-bit** Raspberry Pi OS. Older models are too slow for 4K. |
| USB | Use a **blue USB 3 port** for 4K |
| Power | Use the official power supply. The webcam draws power from the Pi. |
| Screen | The program shows a camera window: use the desktop OS with a monitor or VNC, not plain SSH |
| Speed | Switching to 4K for each photo can take a few seconds |
| Pi Camera Module | The ribbon-cable camera needs `picamera2` and won't work with this code as-is |

Move the code with `git clone` or `scp`. Remove any API key from the code first.
