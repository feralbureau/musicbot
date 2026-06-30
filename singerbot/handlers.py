import asyncio
import os
import time
from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pytgcalls.types import MediaStream, Update, AudioQuality
from pytgcalls.exceptions import NoActiveGroupCall

from singerbot.config import ADMIN_ID, DOWNLOADS_DIR, RADIO_BATCH
from singerbot.core import app, calls, logger
from singerbot.state import active, queues, ban_users, radio_mode
from singerbot.platforms.soundcloud import get_track as sc_get_track, get_stream_url as sc_get_stream_url
from singerbot.utils import (
    is_banned, play_next, download_audio, ensure_assistant_joined,
    send_now_playing, _init_active_state_for_song, sc_id_from_song,
    fetch_radio_ids, get_current_orig_position, _make_transformed_filename,
    _run_ffmpeg_transform_seek_orig, _download_to_file, search_soundcloud_tracks,
)

@app.on_callback_query()
async def callback_handler(_, query: CallbackQuery):
    uid = None
    if query.from_user:
        uid = query.from_user.id
    elif query.message and getattr(query.message, "from_user", None):
        uid = query.message.from_user.id
    if uid and is_banned(uid):
        try:
            await query.answer("You are banned and cannot use this bot.", show_alert=True)
        except Exception:
            try:
                await query.answer()
            except Exception:
                pass
        return
    data = query.data
    cid = query.message.chat.id
    name = query.from_user.first_name.lower() if query.from_user else "unknown"
    if data == "pause":
        try:
            await calls.pause_stream(cid)
            if cid in active and not active[cid].get("paused"):
                active[cid]["paused"] = True
                active[cid]["paused_at"] = time.time()
            await query.answer("paused", show_alert=False)
            await app.send_message(cid, f"{name} paused")
        except Exception:
            await query.answer("cant pause", show_alert=True)
    elif data == "resume":
        try:
            await calls.resume_stream(cid)
            if cid in active and active[cid].get("paused"):
                paused_at = active[cid].pop("paused_at", None)
                if paused_at:
                    elapsed = max(0.0, paused_at - active[cid].get("stream_start_time", paused_at))
                    active[cid]["stream_start_time"] = time.time() - elapsed
                active[cid]["paused"] = False
            await query.answer("resumed", show_alert=False)
            await app.send_message(cid, f"{name} resumed")
        except Exception:
            await query.answer("cant resume", show_alert=True)
    elif data == "skip":
        if cid in active:
            await query.answer("skipping", show_alert=False)
            await app.send_message(cid, f"{name} skipped")
            await play_next(cid)
        else:
            await query.answer("nothing playing", show_alert=True)
    elif data == "end":
        try:
            await calls.leave_group_call(cid)
            if cid in queues:
                queues[cid].clear()
            if cid in active:
                del active[cid]
            await query.answer("stopped", show_alert=False)
            await query.message.edit_caption("**stopped**")
            await app.send_message(cid, f"{name} stopped")
        except Exception:
            await query.answer("not in call", show_alert=True)

@app.on_message(filters.command("start"))
async def start(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("add to group", url="https://t.me/SINGERBOT?startgroup=true")],
            [InlineKeyboardButton("commands", callback_data="help"), InlineKeyboardButton("owner", url="https://t.me/Vclub_Tech")],
        ]
    )
    text = (
        "**music bot**\n\n"
        "use /play to start streaming.\n\n"
        "commands:\n"
        "- /play [song]\n"
        "- /skip\n"
        "- /pause\n"
        "- /resume\n"
        "- /stop\n"
        "- /queue\n"
        "- /speedup (admin)\n"
        "- /slowed (admin)\n"
        "- /radio - toggle radio mode (auto-queue similar tracks)\n"
    )
    try:
        await m.reply_photo("https://telegra.ph/file/2f7debf856695e0a17296.png", caption=text, reply_markup=buttons)
    except Exception:
        await m.reply(text, reply_markup=buttons)

@app.on_callback_query(filters.regex("help"))
async def help_cb(_, q: CallbackQuery):
    uid = q.from_user.id if q.from_user else None
    if uid and is_banned(uid):
        await q.answer()
        return
    await q.answer()
    help_text = (
        "help guide\n\n"
        "`/play [song or link]`\n"
        "`/skip` - skip\n"
        "`/pause` - pause\n"
        "`/resume` - resume\n"
        "`/stop` - stop\n"
        "`/queue` - view queue\n"
        "`/speedup` - pitch up & speed up (admin only)\n"
        "`/slowed` - pitch down & slow down (admin only)\n"
        "`/radio` - toggle radio mode (auto-queue similar tracks)\n"
    )
    await q.message.reply(help_text)

@app.on_message(filters.command("ban") & filters.user(ADMIN_ID))
async def ban_handler(_, m: Message):
    if len(m.command) < 2:
        return
    try:
        target = m.command[1]
        user_obj = await app.get_users(target)
        ban_users.add(user_obj.id)
        await m.reply(f"banned {user_obj.id}")
    except Exception as e:
        await m.reply(f"error: {str(e).lower()}")

@app.on_message(filters.command("unban") & filters.user(ADMIN_ID))
async def unban_handler(_, m: Message):
    if len(m.command) < 2:
        return
    try:
        target = m.command[1]
        user_obj = await app.get_users(target)
        if user_obj.id in ban_users:
            ban_users.remove(user_obj.id)
            await m.reply(f"unbanned {user_obj.id}")
        else:
            await m.reply("not banned")
    except Exception as e:
        await m.reply(f"error: {str(e).lower()}")

@app.on_message(filters.command("search"))
async def search_handler(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    if len(m.command) < 2:
        return await m.reply("usage: `/search [song]`")
    query = m.text.split(None, 1)[1]
    msg = await m.reply("**searching...**")
    try:
        results = await search_soundcloud_tracks(query)
        if not results:
            return await msg.edit("no results found")
        text = "**search results**\n\n"
        for i, res in enumerate(results, 1):
            text += f"{i}. {res['title'].lower()} ({res['duration']})\n"
        await msg.edit(text)
    except Exception as e:
        await msg.edit(f"error: {str(e).lower()}")

@app.on_message(filters.command("play"))
async def play(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    parts = m.text.split(None, 2)
    if len(parts) < 2:
        return await m.reply("usage: `/play [group_id] [song]` or `/play [song]`")
    if parts[1].startswith(("-", "@")):
        try:
            target_chat = await app.get_chat(parts[1])
            cid = target_chat.id
            if len(parts) < 3:
                return await m.reply("usage: `/play [group_id] [song]`")
            q = parts[2]
        except Exception:
            cid = m.chat.id
            q = m.text.split(None, 1)[1]
    else:
        cid = m.chat.id
        q = m.text.split(None, 1)[1]
    msg = await m.reply("**searching...**")
    try:
        try:
            target_chat = await app.get_chat(cid)
            if target_chat.type in ["group", "supergroup"]:
                if not await ensure_assistant_joined(cid):
                    return await msg.edit("bot needs admin to invite assistant")
        except Exception:
            if cid != m.chat.id:
                return await msg.edit("bot is not in that group or id is wrong")
        await msg.edit("**downloading...**")
        song = await download_audio(q)
        if cid not in queues:
            queues[cid] = []
        if cid not in active:
            try:
                state = _init_active_state_for_song(song)
                stream = MediaStream(state["file"], AudioQuality.HIGH)
                try:
                    await calls.join_group_call(cid, stream)
                except Exception as e:
                    msg_err = str(e).lower()
                    if "already joined" in msg_err or "already in group call" in msg_err or "already joined into group call" in msg_err:
                        logger.info(f"Assistant already in call for {cid}, using change_stream to start playback")
                        await calls.change_stream(cid, stream)
                    else:
                        raise
                active[cid] = state
                await msg.delete()
                await send_now_playing(cid, state, [])
                logger.info(f"Started: {state['title']}")
            except NoActiveGroupCall:
                await msg.edit("**no active voice chat found**")
            except Exception as e:
                logger.error(f"play error: {e}")
                await msg.edit(f"error: {str(e).lower()}")
        else:
            queues[cid].append(song)
            await msg.edit(f"queued: {song['title'][:50].lower()}\nposition: {len(queues[cid])}")
    except Exception as e:
        logger.error(f"Command error: {e}")
        await msg.edit(f"error: {str(e)[:100]}")

@app.on_message(filters.command("skip"))
async def skip(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    cid = m.chat.id
    if uid == ADMIN_ID and len(m.command) > 1:
        try:
            target_chat = await app.get_chat(m.command[1])
            cid = target_chat.id
        except Exception:
            pass
    if cid in active:
        await m.reply("**skipped**")
        await play_next(cid)
    else:
        await m.reply("not playing")

@app.on_message(filters.command("pause"))
async def pause(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    cid = m.chat.id
    if uid == ADMIN_ID and len(m.command) > 1:
        try:
            target_chat = await app.get_chat(m.command[1])
            cid = target_chat.id
        except Exception:
            pass
    try:
        await calls.pause_stream(cid)
        if cid in active and not active[cid].get("paused"):
            active[cid]["paused"] = True
            active[cid]["paused_at"] = time.time()
        await m.reply("**paused**")
    except Exception:
        await m.reply("not playing")

@app.on_message(filters.command("resume"))
async def resume(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    cid = m.chat.id
    if uid == ADMIN_ID and len(m.command) > 1:
        try:
            target_chat = await app.get_chat(m.command[1])
            cid = target_chat.id
        except Exception:
            pass
    try:
        await calls.resume_stream(cid)
        if cid in active and active[cid].get("paused"):
            paused_at = active[cid].pop("paused_at", None)
            if paused_at is not None:
                elapsed = max(0.0, paused_at - active[cid].get("stream_start_time", paused_at))
                active[cid]["stream_start_time"] = time.time() - elapsed
            active[cid]["paused"] = False
        await m.reply("**resumed**")
    except Exception:
        await m.reply("not paused")

@app.on_message(filters.command(["stop", "end"]))
async def stop(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    cid = m.chat.id
    if uid == ADMIN_ID and len(m.command) > 1:
        try:
            target_chat = await app.get_chat(m.command[1])
            cid = target_chat.id
        except Exception:
            pass
    try:
        await calls.leave_group_call(cid)
        if cid in queues:
            queues[cid].clear()
        if cid in active:
            del active[cid]
        await m.reply("**stopped**")
    except Exception:
        await m.reply("not in call")

@app.on_message(filters.command("queue"))
async def queue(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    cid = m.chat.id
    if uid == ADMIN_ID and len(m.command) > 1:
        try:
            target_chat = await app.get_chat(m.command[1])
            cid = target_chat.id
        except Exception:
            pass
    if cid not in active:
        return await m.reply("nothing playing")
    text = "**queue**\n\n"
    if cid in queues and queues[cid]:
        for i, s in enumerate(queues[cid], 1):
            text += f"{i}. {s['title'].lower()}\n"
    else:
        text += "empty"
    await m.reply(text)

@app.on_message(filters.command("radio"))
async def radio_handler(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    parts = m.text.split(None, 1)
    cid = m.chat.id
    if len(parts) > 1 and uid == ADMIN_ID:
        try:
            target = await app.get_chat(parts[1])
            cid = target.id
        except Exception:
            pass
    if cid in radio_mode:
        radio_mode.remove(cid)
        await m.reply("radio disabled for this chat")
        return
    radio_mode.add(cid)
    queues.setdefault(cid, [])
    progress_msg = await m.reply("radio: fetching similar tracks...")
    seed_id = None
    if cid in active:
        seed_id = active[cid].get("sc_id")
    if not seed_id and queues.get(cid):
        seed_id = queues[cid][0].get("sc_id")
    if not seed_id:
        radio_mode.discard(cid)
        await progress_msg.edit("cannot enable radio: no reference soundcloud track found. start playing a song first.")
        return
    try:
        ids = await fetch_radio_ids(seed_id, RADIO_BATCH)
        if not ids:
            radio_mode.discard(cid)
            await progress_msg.edit("radio: no similar tracks found")
            return
        added_titles = []
        total = len(ids)
        existing_ids = {s.get("sc_id") for s in queues.get(cid, [])}
        for idx, rid in enumerate(ids, 1):
            if cid not in radio_mode:
                break
            if rid in existing_ids:
                await progress_msg.edit(f"radio: added {len(added_titles)}/{total} (skipping duplicate)\n\n" + ("\n".join(added_titles[-10:]) or ""))
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
                    "thumb": track.get("thumb") or "https://telegra.ph/file/2f7debf856695e0a17296.png",
                    "webpage": track.get("webpage", ""),
                    "sc_id": track["id"],
                }
                queues[cid].append(song)
                existing_ids.add(track["id"])
                title_lower = (song.get("title") or "unknown").lower()
                added_titles.append(title_lower)
                last_list = "\n".join(f"{i}. {t}" for i, t in enumerate(added_titles[-10:], start=max(1, len(added_titles) - 9)))
                await progress_msg.edit(f"radio: added {len(added_titles)}/{total}\n\n{last_list}")
            except Exception as e:
                logger.warning(f"radio download failed for id {rid}: {e}")
                await progress_msg.edit(f"radio: added {len(added_titles)}/{total} (errors may have occurred)\n\n" + ("\n".join(added_titles[-10:]) or ""))
            await asyncio.sleep(1)
        if cid in radio_mode:
            if added_titles:
                await progress_msg.edit(f"radio enabled - added {len(added_titles)} tracks to queue")
            else:
                await progress_msg.edit("radio enabled - no tracks were added")
        else:
            await progress_msg.edit("radio disabled during seeding")
    except Exception as e:
        radio_mode.discard(cid)
        logger.error(f"radio_handler seed failed: {e}")
        try:
            await progress_msg.edit("radio failed to fetch tracks")
        except Exception:
            pass

@calls.on_update()
async def on_end(_, u: Update):
    from pytgcalls.types import StreamAudioEnded
    if isinstance(u, StreamAudioEnded):
        logger.info(f"Stream ended in {u.chat_id}")
        await play_next(u.chat_id)

@app.on_message(filters.command("speedup") & filters.user(ADMIN_ID))
async def speedup_handler(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    parts = m.text.split(None, 2)
    cid = m.chat.id
    if len(parts) > 1:
        maybe = parts[1]
        if maybe.startswith(("-", "@")) or maybe.lstrip("-").isdigit():
            try:
                target = await app.get_chat(maybe)
                cid = target.id
            except Exception:
                pass
    if cid not in active:
        return await m.reply("nothing is playing in the target chat")
    notice = await m.reply("processing speedup... please wait (this may take a few seconds)")
    try:
        state = active[cid]
        cur_pos = get_current_orig_position(state)
        orig = state.get("orig_file")
        if not orig or not os.path.exists(orig):
            await notice.delete()
            return await m.reply("original file not available for seamless transform")
        out = _make_transformed_filename(orig, "speedup")
        factor = 1.2
        await _run_ffmpeg_transform_seek_orig(orig, out, factor, seek=cur_pos, timeout=180)
        stream = MediaStream(out, AudioQuality.HIGH)
        await calls.change_stream(cid, stream)
        state["file"] = out
        state["base_orig_offset"] = float(cur_pos)
        state["stream_start_time"] = time.time()
        state["paused"] = False
        state["play_factor"] = float(factor)
        state["title"] = f"{state.get('title', 'unknown')} (speedup)"
        await notice.delete()
        if m.reply_to_message and m.reply_to_message.from_user:
            ru = m.reply_to_message.from_user
            mention = f"[{ru.first_name}](tg://user?id={ru.id})"
            await m.reply(f"{mention} sped up", parse_mode="markdown")
        else:
            await m.reply("speedup applied")
        logger.info(f"Applied speedup in {cid}: {out} (seek {cur_pos}s)")
    except Exception as e:
        try:
            await notice.delete()
        except Exception:
            pass
        logger.error(f"speedup failed: {e}")
        await m.reply(f"error applying speedup: {e}")

@app.on_message(filters.command("slowed") & filters.user(ADMIN_ID))
async def slowed_handler(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    parts = m.text.split(None, 2)
    cid = m.chat.id
    if len(parts) > 1:
        maybe = parts[1]
        if maybe.startswith(("-", "@")) or maybe.lstrip("-").isdigit():
            try:
                target = await app.get_chat(maybe)
                cid = target.id
            except Exception:
                pass
    if cid not in active:
        return await m.reply("nothing is playing in the target chat")
    notice = await m.reply("processing slowed... please wait (this may take a few seconds)")
    try:
        state = active[cid]
        cur_pos = get_current_orig_position(state)
        orig = state.get("orig_file")
        if not orig or not os.path.exists(orig):
            await notice.delete()
            return await m.reply("original file not available for seamless transform")
        out = _make_transformed_filename(orig, "slowed")
        factor = 0.85
        await _run_ffmpeg_transform_seek_orig(orig, out, factor, seek=cur_pos, timeout=180)
        stream = MediaStream(out, AudioQuality.HIGH)
        await calls.change_stream(cid, stream)
        state["file"] = out
        state["base_orig_offset"] = float(cur_pos)
        state["stream_start_time"] = time.time()
        state["paused"] = False
        state["play_factor"] = float(factor)
        state["title"] = f"{state.get('title', 'unknown')} (slowed)"
        await notice.delete()
        if m.reply_to_message and m.reply_to_message.from_user:
            ru = m.reply_to_message.from_user
            mention = f"[{ru.first_name}](tg://user?id={ru.id})"
            await m.reply(f"{mention} slowed", parse_mode="markdown")
        else:
            await m.reply("slowed applied")
        logger.info(f"Applied slowed in {cid}: {out} (seek {cur_pos}s)")
    except Exception as e:
        try:
            await notice.delete()
        except Exception:
            pass
        logger.error(f"slowed failed: {e}")
        await m.reply(f"error applying slowed: {e}")

@app.on_message(filters.command("restore") & filters.user(ADMIN_ID))
async def restore_handler(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    parts = m.text.split(None, 2)
    cid = m.chat.id
    if len(parts) > 1:
        maybe = parts[1]
        if maybe.startswith(("-", "@")) or maybe.lstrip("-").isdigit():
            try:
                target = await app.get_chat(maybe)
                cid = target.id
            except Exception:
                pass
    if cid not in active:
        return await m.reply("nothing is playing in the target chat")
    notice = await m.reply("restoring normal speed... please wait (this may take a few seconds)")
    try:
        state = active[cid]
        cur_pos = get_current_orig_position(state)
        orig = state.get("orig_file")
        if not orig or not os.path.exists(orig):
            try:
                await notice.delete()
            except Exception:
                pass
            return await m.reply("original file not available for restore")
        out = _make_transformed_filename(orig, "restored")
        factor = 1.0
        await _run_ffmpeg_transform_seek_orig(orig, out, factor, seek=cur_pos, timeout=180)
        stream = MediaStream(out, AudioQuality.HIGH)
        await calls.change_stream(cid, stream)
        state["file"] = out
        state["base_orig_offset"] = float(cur_pos)
        state["stream_start_time"] = time.time()
        state["paused"] = False
        state["play_factor"] = float(factor)
        base_title = state.get("title", "unknown").split(" (")[0]
        state["title"] = f"{base_title} (restored)"
        try:
            await notice.delete()
        except Exception:
            pass
        if m.reply_to_message and m.reply_to_message.from_user:
            ru = m.reply_to_message.from_user
            mention = f"[{ru.first_name}](tg://user?id={ru.id})"
            await m.reply(f"{mention} restored to normal speed", parse_mode="markdown")
        else:
            await m.reply("restored to normal speed")
        logger.info(f"Restored normal speed in {cid}: {out} (seek {cur_pos}s)")
    except Exception as e:
        try:
            await notice.delete()
        except Exception:
            pass
        logger.error(f"restore failed: {e}")
        await m.reply(f"error restoring: {e}")
