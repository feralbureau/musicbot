import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "23550251"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SESSION = os.getenv("SESSION", "")
LOG_GROUP = int(os.getenv("LOG_GROUP", "-1003387540146"))
RADIO_BATCH = int(os.getenv("RADIO_BATCH", "25"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DEFAULT_THUMB = "https://telegra.ph/file/2f7debf856695e0a17296.png"

DOWNLOADS_DIR = "/tmp/singerbot_cache"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
