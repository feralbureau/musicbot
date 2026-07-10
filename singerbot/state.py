import json
import logging
import os

logger = logging.getLogger(__name__)

queues = {}
active = {}
radio_mode = set()
loop_mode = set()
last_np_msg = {}

BANS_FILE = os.path.join(os.getenv("DOWNLOADS_DIR", "/tmp/singerbot_cache"), "bans.json")


def load_bans() -> set:
    try:
        with open(BANS_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()


def save_bans(bans: set) -> None:
    try:
        with open(BANS_FILE, "w") as f:
            json.dump(sorted(int(b) for b in bans), f)
    except Exception as exc:
        logger.warning(f"failed to save bans: {exc}")


ban_users = load_bans()
