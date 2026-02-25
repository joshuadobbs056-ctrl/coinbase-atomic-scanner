import os
import requests
import time

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": text
    }
    requests.post(url, data=data)

send_message("🚀 Atomic scanner is now LIVE.")

while True:
    time.sleep(300)
    send_message("Scanner running... watching for atomic pump setups.")
