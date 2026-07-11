import asyncio
import os
import time
from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pytgcalls.types import MediaStream, Update, AudioQuality
from pytgcalls.exceptions import NoActiveGroupCall

from singerbot.config import ADMIN_ID, DEFAULT_THUMB, DOWNLOADS_DIR, RADIO_BATCH
from singerbot.core import app, calls, logger
from singerbot.state import active, queues, ban_users, radio_mode, loop_mode, save_bans
from singerbot.platforms.soundcloud import get_track as sc_get_track, get_stream_url as sc_get_stream_url
from singerbot.utils import (
    is_banned, play_next, download_audio, ensure_assistant_joined,
    send_now_playing, _init_active_state_for_song, sc_id_from_song,
    fetch_radio_ids, format_duration, get_current_orig_position, _make_transformed_filename,
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
            from singerbot.state import last_np_msg
            if cid in last_np_msg:
                try:
                    await last_np_msg[cid].delete()
                except Exception:
                    pass
                del last_np_msg[cid]
            await query.answer("stopped", show_alert=False)
            await query.message.edit_caption("**stopped**")
            await app.send_message(cid, f"⏹ {name} stopped")
        except Exception:
            await query.answer("not in call", show_alert=True)
    elif data == "toggle_radio":
        if cid in radio_mode:
            radio_mode.remove(cid)
            await query.answer("radio disabled")
        else:
            radio_mode.add(cid)
            await query.answer("radio enabled")
        if cid in active:
            await send_now_playing(cid, active[cid], queues.get(cid, []))
    elif data in ["speedup", "slowed", "restore"]:
        if uid != ADMIN_ID:
            return await query.answer("admin only", show_alert=True)
        if cid not in active:
            return await query.answer("nothing playing", show_alert=True)
        await query.answer("processing speed change...")
        if data == "speedup":
            m_obj = query.message
            m_obj.text = "/speedup" # Mock for command handler
            await speedup_handler(_, m_obj)
        elif data == "slowed":
            m_obj = query.message
            m_obj.text = "/slowed"
            await slowed_handler(_, m_obj)
        elif data == "restore":
            m_obj = query.message
            m_obj.text = "/restore"
            await restore_handler(_, m_obj)
    elif data.startswith("play_sc_"):
        sc_id = data.split("_")[-1]
        await query.answer("adding to queue...")
        try:
            track = await sc_get_track(sc_id)
            if not track:
                return await query.message.edit("could not find track")
            stream_url = await sc_get_stream_url(track["id"])
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
            if cid not in queues:
                queues[cid] = []
            if cid not in active:
                try:
                    target_chat = await app.get_chat(cid)
                    if target_chat.type in ["group", "supergroup"]:
                        if not await ensure_assistant_joined(cid):
                            return await query.message.edit("bot needs admin to invite assistant")
                except Exception:
                    pass
                state = _init_active_state_for_song(song)
                stream = MediaStream(state["file"], AudioQuality.HIGH)
                try:
                    await calls.join_group_call(cid, stream)
                except Exception as e:
                    if "already" in str(e).lower():
                        await calls.change_stream(cid, stream)
                    else:
                        raise
                active[cid] = state
                await send_now_playing(cid, state, [])
            else:
                queues[cid].append(song)
                await app.send_message(cid, f"✅ queued: {song['title']} at position {len(queues[cid])}")
        except Exception as e:
            await app.send_message(cid, f"❌ error: {str(e)}")

@app.on_message(filters.command("start"))
async def start(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ add to group", url="https://t.me/SINGERBOT?startgroup=true")],
            [InlineKeyboardButton("📖 commands", callback_data="help"), InlineKeyboardButton("👨‍💻 owner", url="https://t.me/Vclub_Tech")],
        ]
    )
    text = (
        "**Welcome to SingerBot! 🎵**\n\n"
        "I can stream music from SoundCloud directly into your voice chats! 🚀\n\n"
        "**Basic Commands:**\n"
        "• `/play [song]` - start streaming\n"
        "• `/search [query]` - search for tracks\n"
        "• `/skip` - skip current track\n"
        "• `/pause` / `/resume` - control playback\n"
        "• `/stop` - stop and clear queue\n"
        "• `/queue` - check current queue\n"
        "• `/radio` - toggle auto-queue mode\n"
        "• `/volume` - adjust volume (1-200)\n"
        "• `/loop` - toggle repeat mode\n\n"
        "**Admin Commands:**\n"
        "• `/speedup` - 1.2x speed\n"
        "• `/slowed` - 0.85x speed\n"
        "• `/restore` - normal speed\n\n"
        "Use the buttons below for more info!"
    )
    try:
        await m.reply_photo(DEFAULT_THUMB, caption=text, reply_markup=buttons)
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
        "**📖 SingerBot Help Guide**\n\n"
        "**👤 User Commands:**\n"
        "• `/play [song/url]` - Stream from SoundCloud\n"
        "• `/search [query]` - Search and pick tracks\n"
        "• `/skip` - Skip current track\n"
        "• `/pause` / `/resume` - Control playback\n"
        "• `/stop` - Stop and clear queue\n"
        "• `/queue` - View current tracks\n"
        "• `/radio` - Toggle auto-queue similar tracks\n"
        "• `/volume` - Adjust volume (1-200)\n"
        "• `/remove [pos]` - Remove track at position from queue\n\n"
        "**🔐 Admin Commands:**\n"
        "• `/speedup` - 1.2x speed + pitch up\n"
        "• `/slowed` - 0.85x speed + pitch down\n"
        "• `/restore` - Normal speed\n"
        "• `/ban` / `/unban` - User access control"
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
        save_bans(ban_users)
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
            save_bans(ban_users)
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
    msg = await m.reply("**🔍 searching...**")
    try:
        results = await search_soundcloud_tracks(query)
        if not results:
            return await msg.edit("❌ no results found")

        buttons = []
        for res in results:
            buttons.append([InlineKeyboardButton(f"🎵 {res['title'][:30]} ({res['duration']})", callback_data=f"play_sc_{res['id']}")])

        await msg.edit("**🔍 search results**", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        await msg.edit(f"❌ error: {str(e).lower()}")

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
        from singerbot.state import last_np_msg
        if cid in last_np_msg:
            try:
                await last_np_msg[cid].delete()
            except Exception:
                pass
            del last_np_msg[cid]
        await m.reply("⏹ **stopped and queue cleared**")
    except Exception:
        await m.reply("❌ not in call")

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
        return await m.reply("❌ nothing playing")

    text = f"**📋 current queue for {cid}**\n\n"
    text += f"**🎵 now playing:** {active[cid]['title']}\n\n"

    if cid in queues and queues[cid]:
        for i, s in enumerate(queues[cid], 1):
            text += f"{i}. • {s['title'].lower()}\n"
        text += f"\n**🔢 total tracks:** {len(queues[cid]) + 1}"
    else:
        text += "• _queue is empty_"
    await m.reply(text)


@app.on_message(filters.command(["current", "np"]))
async def current_handler(_, m: Message):
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
        return await m.reply("❌ nothing playing")

    state = active[cid]
    elapsed = get_current_orig_position(state)
    total = state.get("duration", 0)
    pos_str = format_duration(elapsed)
    total_str = format_duration(total) if total else "live"

    text = (
        "**🎵 now playing**\n\n"
        f"**title:** {state['title']}\n"
        f"**artist:** {state['artist']}\n"
        f"**⏳ {pos_str} / {total_str}**\n"
    )
    if cid in radio_mode:
        text += "**📻 radio:** ON ✅\n"
    if cid in loop_mode:
        text += "**🔁 loop:** ON ✅\n"
    if state.get("play_factor", 1.0) != 1.0:
        text += f"**⚡ speed:** {state['play_factor']}x\n"
    if state.get("volume", 100) != 100:
        text += f"**🔊 volume:** {state['volume']}%\n"

    thumb = state.get("thumb") or DEFAULT_THUMB
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏸ pause", callback_data="pause"),
                InlineKeyboardButton("⏭ skip", callback_data="skip"),
            ],
            [
                InlineKeyboardButton("⏹ stop", callback_data="end"),
            ],
        ]
    )
    try:
        await m.reply_photo(thumb, caption=text, reply_markup=buttons)
    except Exception:
        await m.reply(text, reply_markup=buttons)


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
                    "thumb": track.get("thumb") or DEFAULT_THUMB,
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


@app.on_message(filters.command("loop"))
async def loop_handler(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    cid = m.chat.id
    if uid == ADMIN_ID and len(m.command) > 1:
        try:
            target = await app.get_chat(m.command[1])
            cid = target.id
        except Exception:
            pass
    if cid not in active:
        return await m.reply("nothing is playing to loop")
    if cid in loop_mode:
        loop_mode.discard(cid)
        await m.reply("loop disabled")
    else:
        loop_mode.add(cid)
        await m.reply("loop enabled — current track will repeat when it ends")

@app.on_message(filters.command("shuffle"))
async def shuffle_handler(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    cid = m.chat.id
    if uid == ADMIN_ID and len(m.command) > 1:
        try:
            target = await app.get_chat(m.command[1])
            cid = target.id
        except Exception:
            pass
    if cid not in queues or not queues[cid]:
        return await m.reply("queue is empty, nothing to shuffle")
    import random
    random.shuffle(queues[cid])
    await m.reply(f"shuffled {len(queues[cid])} tracks in the queue")


@app.on_message(filters.command("remove"))
async def remove_handler(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    cid = m.chat.id
    if uid == ADMIN_ID and len(m.command) > 2:
        try:
            target = await app.get_chat(m.command[1])
            cid = target.id
            parts = m.command[2:]
        except Exception:
            parts = m.command[1:]
    else:
        parts = m.command[1:]

    if not parts:
        return await m.reply("usage: `/remove [position]` — removes the track at that position from the queue")
    try:
        pos = int(parts[0])
    except ValueError:
        return await m.reply("position must be a number")
    if pos < 1:
        return await m.reply("position must be 1 or greater")
    if cid not in queues or not queues[cid]:
        return await m.reply("queue is empty")
    if pos > len(queues[cid]):
        return await m.reply(f"queue only has {len(queues[cid])} track{'s' if len(queues[cid]) > 1 else ''}, can't remove position {pos}")
    removed = queues[cid].pop(pos - 1)
    title = removed.get("title", "unknown")
    await m.reply(f"removed **{title}** from position {pos}")
    try:
        from singerbot.state import last_np_msg
        if cid in active and cid in last_np_msg:
            await send_now_playing(cid, active[cid], queues.get(cid, []))
    except Exception:
        pass


@app.on_message(filters.command("clear"))
async def clear_handler(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    cid = m.chat.id
    if uid == ADMIN_ID and len(m.command) > 1:
        try:
            target = await app.get_chat(m.command[1])
            cid = target.id
        except Exception:
            pass
    if cid not in queues or not queues.get(cid):
        return await m.reply("queue is already empty")
    count = len(queues[cid])
    queues[cid].clear()
    await m.reply(f"cleared {count} track{'s' if count != 1 else ''} from the queue")
    try:
        if cid in active:
            from singerbot.state import last_np_msg
            if cid in last_np_msg:
                await send_now_playing(cid, active[cid], queues.get(cid, []))
    except Exception:
        pass


@app.on_message(filters.command("restart"))
async def restart_handler(_, m: Message):
    uid = m.from_user.id if m.from_user else None
    if uid and is_banned(uid):
        return
    cid = m.chat.id
    if uid == ADMIN_ID and len(m.command) > 1:
        try:
            target = await app.get_chat(m.command[1])
            cid = target.id
        except Exception:
            pass
    if cid not in active:
        return await m.reply("nothing is playing")
    try:
        state = active[cid]
        new_state = _init_active_state_for_song(state)
        stream = MediaStream(new_state["file"], AudioQuality.HIGH)
        await calls.change_stream(cid, stream)
        active[cid] = new_state
        await send_now_playing(cid, new_state, queues.get(cid, []))
        await m.reply("restarted from the beginning")
    except Exception as e:
        logger.error(f"restart failed: {e}")
        await m.reply(f"error restarting: {e}")


@app.on_message(filters.command("seek"))
async def seek_handler(_, m: Message):
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
                parts = [parts[0], parts[2]] if len(parts) > 2 else [parts[0]]
            except Exception:
                pass

    if cid not in active:
        return await m.reply("nothing is playing to seek in")

    if len(parts) < 2:
        return await m.reply("usage: `/seek [position]` — jump to a position in seconds or mm:ss format")

    pos_arg = parts[-1]
    try:
        if ":" in pos_arg:
            segs = [int(x) for x in pos_arg.split(":")]
            if len(segs) == 2:
                seek_sec = segs[0] * 60 + segs[1]
            elif len(segs) == 3:
                seek_sec = segs[0] * 3600 + segs[1] * 60 + segs[2]
            else:
                return await m.reply("invalid time format, use seconds or mm:ss")
        else:
            seek_sec = int(pos_arg)
    except ValueError:
        return await m.reply("invalid position, use seconds or mm:ss")

    if seek_sec < 0:
        return await m.reply("position must be 0 or greater")

    total_dur = active[cid].get("duration", 0)
    if total_dur and seek_sec >= total_dur:
        return await m.reply(f"position exceeds track duration ({format_duration(total_dur)})")

    notice = await m.reply(f"seeking to {format_duration(seek_sec)}...")
    try:
        state = active[cid]
        orig = state.get("orig_file")
        if not orig or not os.path.exists(orig):
            await notice.delete()
            return await m.reply("original file not available for seeking")

        out = _make_transformed_filename(orig, "seek")
        await _run_ffmpeg_transform_seek_orig(orig, out, factor=1.0, seek=seek_sec, timeout=180)
        stream = MediaStream(out, AudioQuality.HIGH)
        await calls.change_stream(cid, stream)
        state["file"] = out
        state["base_orig_offset"] = float(seek_sec)
        state["stream_start_time"] = time.time()
        state["paused"] = False
        state["play_factor"] = 1.0
        await notice.delete()
        await m.reply(f"⏩ jumped to {format_duration(seek_sec)}")
        logger.info(f"Seek in {cid}: to {seek_sec}s")
    except Exception as e:
        try:
            await notice.delete()
        except Exception:
            pass
        logger.error(f"seek failed: {e}")
        await m.reply(f"error seeking: {str(e)[:100]}")


@app.on_message(filters.command("volume"))
async def volume_handler(_, m: Message):
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
                parts = [parts[0], parts[2]] if len(parts) > 2 else [parts[0]]
            except Exception:
                pass

    if cid not in active:
        return await m.reply("nothing is playing, start a track first")

    if len(parts) < 2:
        cur = active[cid].get("volume", 100)
        return await m.reply(f"current volume: **{cur}%**\nuse `/volume [1-200]` to change it")

    try:
        vol = int(parts[-1])
    except ValueError:
        return await m.reply("volume must be a number between 1 and 200")

    if vol < 1 or vol > 200:
        return await m.reply("volume must be between 1 and 200")

    try:
        await calls.change_volume_call(cid, vol)
        active[cid]["volume"] = vol
        await m.reply(f"volume set to **{vol}%**")
        logger.info(f"Volume changed in {cid}: {vol}%")
    except Exception as e:
        logger.error(f"volume change failed in {cid}: {e}")
        await m.reply(f"error changing volume: {str(e)[:100]}")


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
        state["title"] = f"{state.get('orig_title', 'unknown')} (speedup)"
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
        state["title"] = f"{state.get('orig_title', 'unknown')} (slowed)"
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
        state["title"] = state.get("orig_title", "unknown")
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
