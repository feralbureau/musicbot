import asyncio
import logging
import os
import shlex
import shutil
import time
import uuid

import httpx

from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pytgcalls.exceptions import NoActiveGroupCall
from pytgcalls.types import MediaStream, AudioQuality

from singerbot.config import DOWNLOADS_DIR, RADIO_BATCH, DEFAULT_THUMB
from singerbot.state import queues, active, radio_mode, ban_users, loop_mode, increment_tracks_played
from singerbot.core import app, user, calls, logger
from singerbot.platforms.soundcloud import (
    search_tracks as sc_search_tracks,
    get_stream_url as sc_get_stream_url,
    get_related_tracks as sc_get_related_tracks,
    get_track as sc_get_track,
    resolve_url as sc_resolve_url,
    format_track as sc_format_track,
)


def _is_soundcloud_url(q: str) -> bool:
    return "soundcloud.com" in q or "on.soundcloud.com" in q


async def _resolve_track_from_url(url: str):
    data = await sc_resolve_url(url)
    if not data or data.get("kind") != "track":
        return None
    return sc_format_track(data)


async def download_audio(q: str) -> dict:
    if _is_soundcloud_url(q):
        track = await _resolve_track_from_url(q)
        if not track:
            raise ValueError(f"Could not resolve SoundCloud URL: {q}")
    else:
        results = await sc_search_tracks(q, limit=1)
        if not results:
            raise ValueError(f"No SoundCloud results for: {q}")
        track = results[0]

    if not track.get("streamable"):
        raise ValueError(f"Track '{track['title']}' is not streamable on SoundCloud")

    stream_url = await sc_get_stream_url(track["id"])
    if not stream_url:
        raise ValueError(f"Could not get stream URL for track '{track['title']}'")

    dest = os.path.join(DOWNLOADS_DIR, f"sc_{track['id']}.mp3")
    if not os.path.exists(dest):
        await _download_to_file(stream_url, dest)

    return {
        "file": dest,
        "title": track["title"],
        "artist": track["artist"],
        "duration": track["duration"],
        "thumb": track.get("thumb") or DEFAULT_THUMB,
        "webpage": track.get("webpage", ""),
        "sc_id": track["id"],
    }


async def _download_to_file(url: str, dest: str) -> None:
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(65536):
                    f.write(chunk)


def sc_id_from_song(song: dict) -> str:
    return song.get("sc_id", "")


def record_history(cid, state):
    """Append a track to the in-memory play history for this chat."""
    from singerbot.state import track_history, MAX_HISTORY
    if cid not in track_history:
        track_history[cid] = []
    track_history[cid].append({
        "title": state.get("title", "unknown"),
        "artist": state.get("artist", "unknown"),
        "played_at": time.time(),
    })
    if len(track_history[cid]) > MAX_HISTORY:
        track_history[cid] = track_history[cid][-MAX_HISTORY:]


async def fetch_radio_ids(sc_id: str, max_items: int = RADIO_BATCH) -> list:
    if not sc_id:
        return []
    try:
        related = await sc_get_related_tracks(sc_id, limit=max_items)
        return [t["id"] for t in related if t.get("id")]
    except Exception as exc:
        logger.error(f"fetch_radio_ids failed for seed {sc_id}: {exc}")
        return []


async def ensure_radio_filled(cid):
    if cid not in radio_mode:
        return
    if cid not in queues:
        queues[cid] = []
    if len(queues[cid]) >= 5:
        return
    seed_id = None
    if cid in active:
        seed_id = active[cid].get("sc_id")
    if not seed_id and queues[cid]:
        seed_id = queues[cid][0].get("sc_id")
    if not seed_id:
        return
    try:
        ids = await fetch_radio_ids(seed_id, RADIO_BATCH)
        existing_ids = {s.get("sc_id") for s in queues.get(cid, [])}
        for rid in ids:
            if len(queues[cid]) >= 200:
                break
            if rid in existing_ids:
                continue
            try:
                track = await sc_get_track(rid)
                if not track:
                    continue
                stream_url = await sc_get_stream_url(track["id"])
                if not stream_url:
                    continue
                dest = os.path.join(DOWNLOADS_DIR, f"sc_{track['id']}.mp3")
                if not os.path.exists(dest):
                    await _download_to_file(stream_url, dest)
                song = {
                    "file": dest,
                    "title": track["title"],
                    "artist": track["artist"],
                    "duration": track["duration"],
                    "thumb": track.get("thumb") or DEFAULT_THUMB,
                    "webpage": track.get("webpage", ""),
                    "sc_id": track["id"],
                }
                queues[cid].append(song)
                existing_ids.add(track["id"])
            except Exception as exc:
                logger.warning(f"radio download failed for id {rid}: {exc}")
    except Exception as exc:
        logger.error(f"ensure_radio_filled error for {cid}: {exc}")


async def search_soundcloud_tracks(q: str) -> list:
    results = await sc_search_tracks(q, limit=5)
    return [
        {
            "id": t.get("id"),
            "title": t["title"],
            "duration": t.get("duration_str", format_duration(t.get("duration", 0))),
        }
        for t in results
    ]


def is_banned(uid):
    try:
        return int(uid) in ban_users
    except Exception:
        return False


def format_duration(sec):
    if not sec:
        return "live"
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def build_progress_bar(elapsed: float, total: float, width: int = 12) -> str:
    """Return a visual progress bar string like '▓▓▓▓▓░░░░ 45%'."""
    if total <= 0:
        return "░" * width + " 0%"
    ratio = min(max(elapsed / total, 0.0), 1.0)
    filled = int(ratio * width)
    bar = "▓" * filled + "░" * (width - filled)
    return f"{bar} {int(ratio * 100)}%"


async def cleanup_cache(max_age_hours: int = 24) -> dict:
    """Remove cached audio files older than max_age_hours.

    Returns a dict with keys: removed (count), freed_bytes, errors (count).
    Also removes bans.json-orphaned stale transform files (speedup, slowed, restored, seek).
    """
    removed = 0
    errors = 0
    freed = 0
    now = time.time()
    max_age = max_age_hours * 3600

    if not os.path.isdir(DOWNLOADS_DIR):
        return {"removed": 0, "freed_bytes": 0, "errors": 0}

    for fname in os.listdir(DOWNLOADS_DIR):
        fpath = os.path.join(DOWNLOADS_DIR, fname)
        # Skip bans.json
        if fname == "bans.json" or not os.path.isfile(fpath):
            continue
        try:
            mtime = os.path.getmtime(fpath)
            if now - mtime > max_age:
                sz = os.path.getsize(fpath)
                os.remove(fpath)
                removed += 1
                freed += sz
        except Exception:
            errors += 1
    return {"removed": removed, "freed_bytes": freed, "errors": errors}


async def ensure_assistant_joined(cid):
    try:
        await user.get_chat_member(cid, "me")
        return True
    except UserNotParticipant:
        try:
            link = await app.export_chat_invite_link(cid)
            await user.join_chat(link)
            await asyncio.sleep(2)
            return True
        except Exception:
            return False
    except Exception:
        return False


async def send_now_playing(cid, song, queue_list):
    from singerbot.state import last_np_msg

    # Try to delete last now playing message
    if cid in last_np_msg:
        try:
            await last_np_msg[cid].delete()
        except Exception:
            pass

    caption = (
        "**🎵 now playing**\n\n"
        f"**📌 song :** {song['title']}\n"
        f"**👤 artist :** {song['artist']}\n"
        f"**⏳ duration :** {format_duration(song['duration'])}\n\n"
    )
    if queue_list:
        caption += "**🔜 up next:**\n\n"
        for i, s in enumerate(queue_list[:5], 1):
            caption += f"{i}. {s['title']}\n"
        if len(queue_list) > 5:
            caption += f"\n_plus {len(queue_list) - 5} more_"

    radio_status = "ON ✅" if cid in radio_mode else "OFF ❌"
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏸ pause", callback_data="pause"),
                InlineKeyboardButton("▶️ resume", callback_data="resume"),
            ],
            [
                InlineKeyboardButton("⏭ skip", callback_data="skip"),
                InlineKeyboardButton("⏹ stop", callback_data="end"),
            ],
            [
                InlineKeyboardButton(f"📻 radio: {radio_status}", callback_data="toggle_radio"),
            ],
            [
                InlineKeyboardButton("⏩ speedup", callback_data="speedup"),
                InlineKeyboardButton("⏪ slowed", callback_data="slowed"),
                InlineKeyboardButton("🔄 restore", callback_data="restore"),
            ]
        ]
    )
    thumb = song.get("thumb") or DEFAULT_THUMB
    try:
        msg = await app.send_photo(cid, thumb, caption=caption, reply_markup=buttons)
        last_np_msg[cid] = msg
    except Exception as exc:
        logger.warning(f"Photo send failed: {exc}, using text")
        msg = await app.send_message(cid, caption, reply_markup=buttons)
        last_np_msg[cid] = msg


def _make_transformed_filename(src: str, suffix: str) -> str:
    base = os.path.basename(src)
    name, ext = os.path.splitext(base)
    uniq = uuid.uuid4().hex[:8]
    return os.path.join(DOWNLOADS_DIR, f"{name}_{suffix}_{uniq}{ext}")


def get_current_orig_position(state: dict) -> float:
    base = state.get("base_orig_offset", 0.0)
    stream_start = state.get("stream_start_time", time.time())
    play_factor = state.get("play_factor", 1.0)
    if state.get("paused"):
        paused_at = state.get("paused_at", stream_start)
        elapsed = max(0.0, paused_at - stream_start)
    else:
        elapsed = max(0.0, time.time() - stream_start)
    return base + (elapsed * float(play_factor))


async def _run_ffmpeg_transform_seek_orig(
    orig_path: str, out_path: str, factor: float, seek: float, timeout: int = 120
):
    atempo = max(0.5, min(2.0, factor))
    asetrate_expr = f"asetrate=44100*{factor}"
    af_filter = f"{asetrate_expr},aresample=44100,atempo={atempo}"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(float(seek)),
        "-i", orig_path,
        "-af", af_filter,
        "-vn", out_path,
    ]
    logger.info(f"Running ffmpeg: {' '.join(shlex.quote(p) for p in cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise Exception("ffmpeg timed out")
    if proc.returncode != 0:
        err = stderr.decode(errors="ignore").strip()
        raise Exception(f"ffmpeg error: {err}")
    return out_path


def _init_active_state_for_song(song: dict) -> dict:
    return {
        "orig_file": song["file"],
        "orig_title": song.get("title", "unknown"),
        "file": song["file"],
        "title": song.get("title", "unknown"),
        "artist": song.get("artist", "unknown"),
        "duration": song.get("duration", 0),
        "thumb": song.get("thumb"),
        "webpage": song.get("webpage"),
        "sc_id": song.get("sc_id", ""),
        "base_orig_offset": 0.0,
        "stream_start_time": time.time(),
        "paused": False,
        "paused_at": None,
        "play_factor": 1.0,
        "volume": 100,
    }


async def play_next(cid, _retries: int = 0):
    if cid not in queues or not queues[cid]:
        if cid in loop_mode and cid in active:
            old = active[cid]
            queues[cid] = [dict(old)]
        else:
            logger.info(f"Queue empty in {cid}")
            if cid in active:
                del active[cid]
            return
    if _retries > 3:
        logger.error(f"play_next: too many retries for {cid}, stopping")
        queues[cid].clear()
        if cid in active:
            del active[cid]
        return
    s = queues[cid].pop(0)
    try:
        state = _init_active_state_for_song(s)
        stream = MediaStream(state["file"], AudioQuality.HIGH)
        await calls.change_stream(cid, stream)
        active[cid] = state
        increment_tracks_played()
        record_history(cid, state)
        await send_now_playing(cid, state, queues.get(cid, []))
        logger.info(f"Playing: {state['title']}")
        try:
            await ensure_radio_filled(cid)
        except Exception as exc:
            logger.warning(f"ensure_radio_filled failed in play_next for {cid}: {exc}")
    except Exception as exc:
        logger.error(f"play next error: {exc}")
        await play_next(cid, _retries + 1)
