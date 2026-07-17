import json
import logging
import os
import time

from singerbot.config import DOWNLOADS_DIR

logger = logging.getLogger(__name__)

queues = {}
active = {}
radio_mode = set()
loop_mode = set()
last_np_msg = {}

bot_start_time = time.time()
_tracks_played = 0


def tracks_played_count() -> int:
    return _tracks_played


def increment_tracks_played() -> None:
    global _tracks_played
    _tracks_played += 1

BANS_FILE = os.path.join(DOWNLOADS_DIR, "bans.json")


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
