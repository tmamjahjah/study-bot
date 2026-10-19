import os
import json
import asyncio
from telethon import TelegramClient, events, Button
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from telethon.errors import FloodWaitError, MessageNotModifiedError

# 1. Your Telegram API credentials
API_ID = 35911624
API_HASH = '84333e05287cb06b1da68f6571cadaab'

# 2. Your @MyStudying_last_bot Token
BOT_TOKEN = '8820981008:AAFDATbel0KyTpLT3tKiyq4mK3B7603O4AQ'

CONFIG_FILE = 'groups_config.json'
STATE_FILE = 'bot_chat_synced.json'
HISTORY_SCAN_LIMIT = 2000

user_client = TelegramClient('hashtag_watcher_session', API_ID, API_HASH)
bot_client = TelegramClient('control_bot_session', API_ID, API_HASH)

# Track who is currently typing hashtags: {user_id: {"id": chat_key, "title": title}}
pending_hashtag_input = {}

# Queue and Lock to safely bridge media from user_client to bot_client in private chat
bot_media_queue = asyncio.Queue()
media_lock = asyncio.Lock()
is_forwarding_media = False


def load_json(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    return json.loads(content)
        except Exception:
            pass
    return {}


def save_json(filepath, data):
    temp_file = filepath + '.tmp'
    with open(temp_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, filepath)


def get_monitored_groups(cfg):
    """Returns only the group dictionary entries (ignoring metadata keys like _target_chat_id)."""
    return {k: v for k, v in cfg.items() if not k.startswith('_') and isinstance(v, dict)}


def get_target_chat_id(cfg, fallback_id):
    """Gets the shared group ID where lectures should be posted (falls back to owner DM if not set yet)."""
    return cfg.get("_target_chat_id", fallback_id)


def generate_message_link(chat, chat_key, message_id):
    username = getattr(chat, 'username', None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    clean_id = str(chat_key).replace('-100', '').replace('-', '')
    return f"https://t.me/c/{clean_id}/{message_id}"


def format_post(matched_tags, group_title, text, link):
    tags_str = ", ".join(matched_tags)
    return (
        f"📌 **الهاشتاغ:** {tags_str}\n"
        f"👥 **المجموعة:** {group_title}\n"
        f"────────────────────\n"
        f"{text}\n"
        f"────────────────────\n"
        f"🔗 [👉 اضغط هنا للانتقال إلى الرسالة الأصلية في المجموعة]({link})"
    )


def find_matching_tags(text, target_tags):
    if not text:
        return []
    return [tag for tag in target_tags if tag in text]


async def sync_single_message(msg, chat, chat_key, group_cfg, target_chat_id, owner_id, state, bot_username):
    """Sends or updates the matched message inside the shared target group (or DM)."""
    global is_forwarding_media

    text = msg.raw_text or ""
    matched_tags = find_matching_tags(text, group_cfg["hashtags"])
    if not matched_tags:
        return False

    if "messages" not in state:
        state["messages"] = {}

    unique_key = f"{chat_key}:{msg.id}"
    link = generate_message_link(chat, chat_key, msg.id)
    formatted_body = format_post(matched_tags, group_cfg["title"], text, link)
    jump_button = [[Button.url("🔗 الانتقال للرسالة الأصلية في المجموعة", link)]]

    # If already sent -> Check if text was edited in the source group
    if unique_key in state["messages"]:
        saved_entry = state["messages"][unique_key]
        target_dest = saved_entry.get("target_chat_id", target_chat_id)
        if saved_entry.get("text") != text and saved_entry.get("bot_msg_id"):
            try:
                body_to_edit = formatted_body if len(formatted_body) <= 1024 or not msg.media else formatted_body[:1020] + "..."
                await bot_client.edit_message(
                    target_dest,
                    saved_entry["bot_msg_id"],
                    body_to_edit,
                    buttons=jump_button,
                    link_preview=False
                )
                saved_entry["text"] = text
                save_json(STATE_FILE, state)
                return True
            except MessageNotModifiedError:
                saved_entry["text"] = text
                save_json(STATE_FILE, state)
            except Exception:
                pass
        return False

    # New message -> Send it to target_chat_id (the shared group!)
    while True:
        try:
            if msg.media:
                async with media_lock:
                    while not bot_media_queue.empty():
                        bot_media_queue.get_nowait()

                    is_forwarding_media = True
                    try:
                        # 1. Silently forward media from user_client to bot's private DM
                        user_fwd = await user_client.forward_messages(bot_username, msg)
                        # 2. Bot catches the media handle in private DM
                        bot_rx = await asyncio.wait_for(bot_media_queue.get(), timeout=15.0)
                    finally:
                        is_forwarding_media = False

                    # 3. Clean up the temporary forward in private DM
                    try:
                        await bot_client.delete_messages(owner_id, [bot_rx.id])
                    except Exception:
                        pass
                    try:
                        fwd_id = user_fwd.id if hasattr(user_fwd, 'id') else user_fwd[0].id
                        await user_client.delete_messages(bot_username, [fwd_id])
                    except Exception:
                        pass

                # 4. Bot sends the file + button into the shared group (target_chat_id)
                if len(formatted_body) <= 1024:
                    sent_msg = await bot_client.send_message(
                        target_chat_id,
                        formatted_body,
                        file=bot_rx.media,
                        buttons=jump_button,
                        link_preview=False
                    )
                else:
                    short_header = (
                        f"📌 **الهاشتاغ:** {', '.join(matched_tags)}\n"
                        f"👥 **المجموعة:** {group_cfg['title']}\n"
                        f"📎 *(النص طويل، مرفق في الرسالة التالية 👇)*"
                    )
                    await bot_client.send_message(
                        target_chat_id,
                        short_header,
                        file=bot_rx.media,
                        buttons=jump_button
                    )
                    sent_msg = await bot_client.send_message(
                        target_chat_id,
                        formatted_body[:4096],
                        buttons=jump_button,
                        link_preview=False
                    )
            else:
                sent_msg = await bot_client.send_message(
                    target_chat_id,
                    formatted_body[:4096],
                    buttons=jump_button,
                    link_preview=False
                )

            state["messages"][unique_key] = {
                "bot_msg_id": sent_msg.id,
                "target_chat_id": target_chat_id,
                "text": text
            }
            save_json(STATE_FILE, state)
            print(f"✅ Sent #{msg.id} ({', '.join(matched_tags)}) from '{group_cfg['title']}' to Target Chat ({target_chat_id})")
            await asyncio.sleep(1.5)
            return True

        except FloodWaitError as e:
            is_forwarding_media = False
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            is_forwarding_media = False
            print(f"⚠️ Error sending message #{msg.id}: {e}")
            return False


async def scan_all_groups_history(config, target_chat_id, owner_id, state, bot_username):
    total_added = 0
    monitored = get_monitored_groups(config)

    for chat_key, group_cfg in monitored.items():
        tags = group_cfg["hashtags"]
        try:
            chat_id_int = int(chat_key) if chat_key.lstrip('-').isdigit() else chat_key
            chat = await user_client.get_entity(chat_id_int)
        except Exception as e:
            print(f"⚠️ Cannot access {group_cfg['title']}: {e}")
            continue

        matching_msgs = []
        async for msg in user_client.iter_messages(chat, limit=HISTORY_SCAN_LIMIT):
            if msg.raw_text and find_matching_tags(msg.raw_text, tags):
                matching_msgs.append(msg)

        matching_msgs.reverse()  # Oldest to newest
        for msg in matching_msgs:
            if await sync_single_message(msg, chat, chat_key, group_cfg, target_chat_id, owner_id, state, bot_username):
                total_added += 1

    return total_added


def main_menu_buttons():
    return [
        [Button.inline("📋 المجموعات المراقبة", b"menu_groups"),
         Button.inline("➕ إضافة / تعديل مجموعة", b"menu_add_0")],
        [Button.inline("🗑️ حذف مجموعة", b"menu_remove"),
         Button.inline("🔄 فحص الرسائل القديمة", b"menu_scan")],
        [Button.inline("📍 اعتماد هذه المحادثة للنشر", b"menu_set_here")],
        [Button.inline("♻️ إعادة نسخ الكل من الصفر", b"menu_force_rescan")]
    ]


async def setup_slash_menu():
    commands = [
        BotCommand(command="start", description="🏠 فتح لوحة التحكم الرئيسية بالأزرار"),
        BotCommand(command="setgroup", description="📍 اعتماد هذه المجموعة لوصول المحاضرات تلقائياً"),
        BotCommand(command="groups", description="📋 عرض المجموعات والهاشتاغات الحالية"),
        BotCommand(command="add", description="➕ اختيار مجموعة من القائمة لإضافتها"),
        BotCommand(command="remove", description="🗑️ إيقاف مراقبة مجموعة"),
        BotCommand(command="scan", description="🔄 فحص الرسائل القديمة والتعديلات الآن")
    ]
    await bot_client(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=commands
    ))


async def main():
    await user_client.start()
    await bot_client.start(bot_token=BOT_TOKEN)

    me = await user_client.get_me()
    owner_id = me.id
    bot_me = await bot_client.get_me()
    bot_username = bot_me.username

    state = load_json(STATE_FILE)

    await setup_slash_menu()

    # 1. Watch all configured groups in real time (Automatic!)
    @user_client.on(events.NewMessage)
    @user_client.on(events.MessageEdited)
    async def live_handler(event):
        current_cfg = load_json(CONFIG_FILE)
        monitored = get_monitored_groups(current_cfg)
        chat_key = str(event.chat_id)
        if chat_key not in monitored:
            return
        target_chat_id = get_target_chat_id(current_cfg, owner_id)
        chat = await event.get_chat()
        await sync_single_message(
            event.message,
            chat,
            chat_key,
            monitored[chat_key],
            target_chat_id,
            owner_id,
            state,
            bot_username
        )

    # 2. Handle commands, media bridging, and hashtag input from ANY member in the group
    @bot_client.on(events.NewMessage)
    async def bot_message_handler(event):
        # Catch media forwarded privately by owner_id for bridging
        if event.is_private and event.sender_id == owner_id and is_forwarding_media and (event.message.fwd_from or event.message.media):
            await bot_media_queue.put(event.message)
            return

        sender_id = event.sender_id
        raw_text = (event.raw_text or "").strip()
        # Strip @bot_username from group slash commands like /start@MyStudying_last_bot
        cmd = raw_text.split('@')[0].strip() if raw_text.startswith('/') else raw_text

        # Auto-save group as target if someone runs a command inside a group and no target group is set yet
        cfg = load_json(CONFIG_FILE)
        if not event.is_private and "_target_chat_id" not in cfg:
            cfg["_target_chat_id"] = event.chat_id
            save_json(CONFIG_FILE, cfg)

        # If this user was asked to send hashtags for a selected group
        if sender_id in pending_hashtag_input and not raw_text.startswith('/'):
            target_info = pending_hashtag_input.pop(sender_id)
            chat_key = target_info["id"]
            title = target_info["title"]

            tags = [
                (t.strip() if t.strip().startswith('#') else f"#{t.strip()}")
                for t in raw_text.replace('،', ',').split(',') if t.strip()
            ]
            cfg = load_json(CONFIG_FILE)
            cfg[chat_key] = {"title": title, "hashtags": tags}
            # Also ensure the chat where they configured it becomes the target chat
            cfg["_target_chat_id"] = event.chat_id
            save_json(CONFIG_FILE, cfg)

            target_chat_id = get_target_chat_id(cfg, event.chat_id)
            wait_msg = await event.reply(
                f"✅ **تم الحفظ بنجاح!**\n"
                f"👥 المجموعة: **{title}**\n"
                f"🏷️ الهاشتاغات: {', '.join(tags)}\n\n"
                f"⏳ جاري نسخ المحاضرات والرسائل السابقة إلى هذه المحادثة..."
            )
            added = await scan_all_groups_history(cfg, target_chat_id, owner_id, state, bot_username)
            await wait_msg.edit(
                f"🎉 **اكتمل الإعداد والنسخ!**\n"
                f"👥 المجموعة: **{title}**\n"
                f"🏷️ الهاشتاغات: {', '.join(tags)}\n"
                f"📥 الرسائل التي تم جلبها: `{added}`",
                buttons=main_menu_buttons()
            )
            return

        if cmd in ('/start', '/help'):
            cfg = load_json(CONFIG_FILE)
            if not event.is_private:
                cfg["_target_chat_id"] = event.chat_id
                save_json(CONFIG_FILE, cfg)
            await event.reply(
                "👋 **أهلاً بكم في بوت التفريغات المشترك!**\n\n"
                "جميع المحاضرات والملفات الجديدة ستصل **تلقائياً وفوراً** إلى هذه المحادثة مع زر الانتقال للرسالة الأصلية.\n"
                "يمكن لأي عضو في المجموعة التحكم بالبوت من الأزرار أدناه:",
                buttons=main_menu_buttons()
            )
        elif cmd == '/setgroup':
            cfg = load_json(CONFIG_FILE)
            cfg["_target_chat_id"] = event.chat_id
            save_json(CONFIG_FILE, cfg)
            await event.reply(
                "✅ **تم اعتماد هذه المحادثة!**\n"
                "جميع المحاضرات والهاشتاغات الجديدة والقديمة ستصل إلى هنا مباشرة.",
                buttons=main_menu_buttons()
            )
        elif cmd == '/groups':
            await show_groups_view(event, is_callback=False)
        elif cmd == '/add':
            await show_add_groups_page(event, page=0, is_callback=False)
        elif cmd == '/remove':
            await show_remove_menu(event, is_callback=False)
        elif cmd == '/scan':
            msg = await event.reply("🔄 جاري فحص الرسائل القديمة وجلبها إلى هنا...")
            cfg = load_json(CONFIG_FILE)
            target_chat_id = get_target_chat_id(cfg, event.chat_id)
            added = await scan_all_groups_history(cfg, target_chat_id, owner_id, state, bot_username)
            await msg.edit(f"✅ انتهى الفحص! تم جلب/تحديث `{added}` رسالة في هذه المحادثة.", buttons=main_menu_buttons())

    # 3. Handle button clicks from ANY member in the group
    @bot_client.on(events.CallbackQuery)
    async def callback_handler(event):
        sender_id = event.sender_id
        data = event.data.decode('utf-8')

        if data == "menu_home":
            pending_hashtag_input.pop(sender_id, None)
            await event.edit(
                "🏠 **لوحة التحكم الرئيسية:**\nاختر من الأزرار أدناه:",
                buttons=main_menu_buttons()
            )

        elif data == "menu_set_here":
            cfg = load_json(CONFIG_FILE)
            cfg["_target_chat_id"] = event.chat_id
            save_json(CONFIG_FILE, cfg)
            await event.answer("✅ تم اعتماد هذه المجموعة لوصول جميع المحاضرات!", alert=True)
            await event.edit(
                "✅ **تم تفعيل النشر التلقائي في هذه المجموعة!**\n"
                "أي محاضرة جديدة تُنشر في المجموعات المراقبة ستصل إلى هنا فوراً.",
                buttons=main_menu_buttons()
            )

        elif data == "menu_groups":
            await show_groups_view(event, is_callback=True)

        elif data.startswith("menu_add_"):
            page = int(data.split("_")[-1])
            await show_add_groups_page(event, page=page, is_callback=True)

        elif data.startswith("pick_"):
            chat_key = data.split("_", 1)[1]
            dialogs = await user_client.get_dialogs()
            title = chat_key
            for d in dialogs:
                if str(d.id) == chat_key:
                    title = d.name
                    break

            pending_hashtag_input[sender_id] = {"id": chat_key, "title": title}
            cfg = load_json(CONFIG_FILE)
            current_tags = ", ".join(cfg.get(chat_key, {}).get("hashtags", []))
            extra_note = f"\n*(الهاشتاغات الحالية: {current_tags})*" if current_tags else ""

            await event.edit(
                f"👥 اخترت المجموعة: **{title}**{extra_note}\n\n"
                f"✍️ **أرسل الآن في المحادثة الهاشتاغات المطلوبة لهذه المجموعة**\n"
                f"افصل بينها بفاصلة، مثال:\n"
                f"`#تفريغات_صيدلية_٢, #لوح_صيدلية_٢`",
                buttons=[[Button.inline("🔙 إلغاء والعودة", b"menu_home")]]
            )

        elif data == "menu_remove":
            await show_remove_menu(event, is_callback=True)

        elif data.startswith("del_"):
            chat_key = data.split("_", 1)[1]
            cfg = load_json(CONFIG_FILE)
            if chat_key in cfg:
                removed_title = cfg.pop(chat_key)["title"]
                save_json(CONFIG_FILE, cfg)
                await event.answer(f"تم حذف {removed_title}", alert=True)
            await show_remove_menu(event, is_callback=True)

        elif data == "menu_scan":
            await event.edit("🔄 جاري فحص الرسائل القديمة وجلبها إلى هنا... يرجى الانتظار.")
            cfg = load_json(CONFIG_FILE)
            target_chat_id = get_target_chat_id(cfg, event.chat_id)
            added = await scan_all_groups_history(cfg, target_chat_id, owner_id, state, bot_username)
            await event.edit(
                f"✅ **انتهى الفحص!**\nتم جلب/تحديث `{added}` رسالة مباشرة في هذه المحادثة.",
                buttons=main_menu_buttons()
            )

        elif data == "menu_force_rescan":
            state["messages"] = {}
            save_json(STATE_FILE, state)
            cfg = load_json(CONFIG_FILE)
            cfg["_target_chat_id"] = event.chat_id
            save_json(CONFIG_FILE, cfg)
            await event.edit("♻️ تم تصفير الذاكرة! جاري إعادة نسخ جميع المحاضرات والرسائل القديمة إلى هذه المجموعة الآن...")
            added = await scan_all_groups_history(cfg, event.chat_id, owner_id, state, bot_username)
            await bot_client.send_message(
                event.chat_id,
                f"✅ **اكتمل النسخ الشامل من الصفر!**\nتم جلب `{added}` رسالة ومحاضرة إلى هذه المجموعة.",
                buttons=main_menu_buttons()
            )

    async def show_groups_view(event, is_callback=True):
        cfg = load_json(CONFIG_FILE)
        monitored = get_monitored_groups(cfg)
        if not monitored:
            text = "⚠️ لا توجد مجموعات مراقبة حالياً.\nاضغط على **➕ إضافة / تعديل مجموعة** لاختيار مجموعاتك."
        else:
            text = "📋 **المجموعات والهاشتاغات المراقبة حالياً:**\n\n"
            for cid, info in monitored.items():
                text += f"• **{info['title']}**\n  🏷️ `{', '.join(info['hashtags'])}`\n\n"
        btns = [[Button.inline("➕ إضافة / تعديل", b"menu_add_0"),
                 Button.inline("🗑️ حذف مجموعة", b"menu_remove")],
                [Button.inline("🔙 القائمة الرئيسية", b"menu_home")]]
        if is_callback:
            await event.edit(text, buttons=btns)
        else:
            await event.reply(text, buttons=btns)

    async def show_add_groups_page(event, page=0, is_callback=True):
        dialogs = await user_client.get_dialogs()
        # Exclude the shared target group itself from the list if desired
        groups = [d for d in dialogs if (d.is_group or d.is_channel) and d.id != event.chat_id]
        per_page = 8
        start = page * per_page
        end = start + per_page
        current_slice = groups[start:end]

        btns = []
        for g in current_slice:
            btns.append([Button.inline(f"👥 {g.name[:35]}", f"pick_{g.id}".encode('utf-8'))])

        nav = []
        if page > 0:
            nav.append(Button.inline("⬅️ السابق", f"menu_add_{page - 1}".encode('utf-8')))
        if end < len(groups):
            nav.append(Button.inline("التالي ➡️", f"menu_add_{page + 1}".encode('utf-8')))
        if nav:
            btns.append(nav)
        btns.append([Button.inline("🔙 القائمة الرئيسية", b"menu_home")])

        text = "➕ **اختر المجموعة التي تريد مراقبتها بالضغط على اسمها:**"
        if is_callback:
            await event.edit(text, buttons=btns)
        else:
            await event.reply(text, buttons=btns)

    async def show_remove_menu(event, is_callback=True):
        cfg = load_json(CONFIG_FILE)
        monitored = get_monitored_groups(cfg)
        if not monitored:
            text = "⚠️ لا توجد مجموعات لحذفها."
            btns = [[Button.inline("🔙 القائمة الرئيسية", b"menu_home")]]
        else:
            text = "🗑️ **اضغط على أي مجموعة لإيقاف مراقبتها فوراً:**"
            btns = [
                [Button.inline(f"❌ {info['title'][:32]}", f"del_{cid}".encode('utf-8'))]
                for cid, info in monitored.items()
            ]
            btns.append([Button.inline("🔙 القائمة الرئيسية", b"menu_home")])

        if is_callback:
            await event.edit(text, buttons=btns)
        else:
            await event.reply(text, buttons=btns)

    print(f"🚀 Shared Group Bot is LIVE! Add @{bot_username} to your group and send /start")
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )


if __name__ == '__main__':
    asyncio.run(main())