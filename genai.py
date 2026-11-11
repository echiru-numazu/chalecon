import requests
import os
from datetime import datetime
from fpdf import FPDF
import google.generativeai as genai
import PIL.Image

# ==========================================
# 1. System and Google GenAI Setup
# ==========================================
PHONE_IP = "172.31.12.36" # Change to your phone's IP
PORT = "8080"
# Using /photoaf.jpg ensures the camera auto-focuses before capturing[cite: 2]
CAMERA_URL = f"http://{PHONE_IP}:{PORT}/photoaf.jpg"

# Configure API Key via the SDK
genai.configure(api_key="AQ.Ab8RN6IU_aiNddtOoie5ATQWyXl-CqLt7qTyXcWDgDQsgfDCew")
# Initialize the model directly (no need to worry about URL versions)
model = genai.GenerativeModel('gemini-3.1-flash-lite')

# ==========================================
# 2. Create Session Folder
# ==========================================
session_name = datetime.now().strftime("Class_%Y%m%d_%H%M")
os.makedirs(session_name, exist_ok=True)
print(f"📁 Class session started! Data will be saved in: {session_name}")

# ==========================================
# 3. Capture Loop
# ==========================================
image_count = 0
image_paths = []

while True:
    action = input(f"\n[📸 Image {image_count+1}] Press Enter to capture, or type 'end' to finish: ")
    
    if action.lower() == 'end':
        break
        
    print("Sending capture command to phone...")
    try:
        # Send HTTP GET request to pull the image[cite: 2]
        response = requests.get(CAMERA_URL, timeout=10)
        
        if response.status_code == 200:
            image_count += 1
            img_path = os.p ath.join(session_name, f"board_{image_count}.jpg")
            with open(img_path, "wb") as f:
                f.write(response.content)
            image_paths.append(img_path)
            print(f"✅ Successfully saved {img_path}!")
        else:
            print(f"❌ Capture failed. Status Code: {response.status_code}")
    except Exception as e:
        print(f"❌ Connection error. Check your Wi-Fi: {e}")

# ==========================================
# 4. Send Data to Gemini via SDK
# ==========================================
if not image_paths:
    print("Session ended (No images captured).")
    exit()

print(f"\n🚀 Sending {len(image_paths)} images to Gemini for summary...")

# Create a list containing the text prompt and all images
prompt_parts = [
    "These are whiteboard images from a class. Please summarize the content, organize it into bullet points, and structure it well for exam review."
]

# Open images with PIL and append them to the prompt list
for img_path in image_paths:
    img = PIL.Image.open(img_path)
    prompt_parts.append(img)

try:
    # Generate content in a single call using the SDK
    ai_response = model.generate_content(prompt_parts)
    ai_text = ai_response.text
    print("\n✅ AI analysis complete! Generating PDF...")
    
    # ==========================================
    # 5. Generate PDF Report
    # ==========================================
    txt_path = os.path.join(session_name, "summary.txt")
    with open(txt_path, "w", encoding="utf-8") as text_file:
        text_file.write(ai_text)
    
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    # Encode and decode to handle potential unsupported characters
    safe_text = ai_text.encode('latin-1', 'replace').decode('latin-1')
    pdf.multi_cell(0, 10, safe_text)
    
    pdf_path = os.path.join(session_name, "Summary_Report.pdf")
    pdf.output(pdf_path)
    print(f"🎉 Success! Summary report saved to: {pdf_path}")

except Exception as e:
    print(f"❌ AI processing error: {e}")
