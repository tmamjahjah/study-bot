import os
import json
import asyncio
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from telethon.errors import FloodWaitError, MessageNotModifiedError

# Safely loaded from encrypted GitHub Secrets!
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION_STRING = os.environ["SESSION_STRING"]

CONFIG_FILE = 'groups_config.json'
STATE_FILE = 'bot_chat_synced.json'
HISTORY_SCAN_LIMIT = 2000

user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
bot_client = TelegramClient(StringSession(), API_ID, API_HASH)

# Track user text input states
pending_user_input = {}

# Queue and Lock to safely bridge media from user_client to bot_client in private chat
bot_media_queue = asyncio.Queue()
media_lock = asyncio.Lock()
is_forwarding_media = False

ACADEMIC_YEARS = [
    "السنة الأولى",
    "السنة الثانية",
    "السنة الثالثة",
    "السنة الرابعة",
    "السنة الخامسة"
]

CATEGORY_LABELS = {
    "lectures": "📝 تفريغات",
    "boards": "🖼️ صور لوح",
    "records": "🎙️ تسجيلات"
}

DEFAULT_SUBJECTS_TREE = {
    "السنة الخامسة": {
        "كيمياء صيدلية 2": {
            "lectures": ["#تفريغات_صيدلية_٢", "#تفريغات_كيمياء_صيدلية_2"],
            "boards": ["#لوح_صيدلية_٢", "#لوح_كيمياء_صيدلية_2"],
            "records": ["#تسجيلات_صيدلية_٢", "#تسجيل_صيدلية_٢"]
        }
    },
    "السنة الرابعة": {}
}


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


def ensure_ignored_dict(state):
    """Ensures state['ignored_messages'] is a dict for storing snippets/metadata."""
    ignored = state.get("ignored_messages", {})
    if isinstance(ignored, list):
        state["ignored_messages"] = {
            k: {"snippet": f"رسالة ({k})", "group_title": "مجموعة مراقبة", "link": ""}
            for k in ignored
        }
        save_json(STATE_FILE, state)
    elif not isinstance(ignored, dict):
        state["ignored_messages"] = {}
    return state["ignored_messages"]


def make_snippet(text, max_len=45):
    if not text:
        return "ملف / وسائط بدون نص"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    chosen = lines[0] if lines else "رسالة"
    for ln in lines:
        if not ln.startswith('#'):
            chosen = ln
            break
    return chosen[:max_len] + ("..." if len(chosen) > max_len else "")


def get_monitored_groups(cfg):
    """Returns only the group dictionary entries (ignoring metadata keys starting with _)."""
    return {k: v for k, v in cfg.items() if not k.startswith('_') and isinstance(v, dict)}


def get_subjects_tree(cfg):
    """Returns the subjects dictionary from config, initializing with defaults if missing."""
    if "_subjects" not in cfg:
        cfg["_subjects"] = DEFAULT_SUBJECTS_TREE
        save_json(CONFIG_FILE, cfg)
    return cfg["_subjects"]


def get_target_chat_id(cfg, fallback_id):
    """Gets the shared group ID where lectures should be posted (falls back to owner DM if not set yet)."""
    return cfg.get("_target_chat_id", fallback_id)


def generate_message_link(chat, chat_key, message_id):
    username = getattr(chat, 'username', None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    clean_id = str(chat_key).replace('-100', '').replace('-', '')
    return f"https://t.me/c/{clean_id}/{message_id}"


def build_message_buttons(link, unique_key):
    """Builds the bottom button row with a subtle ⚙️ icon for safe exception/removal."""
    return [
        [
            Button.url("🔗 الرسالة الأصلية في المجموعة", link),
            Button.inline("⚙️", f"exc_ask|{unique_key}".encode('utf-8'))
        ]
    ]


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

    if "messages" not in state:
        state["messages"] = {}
    ignored_dict = ensure_ignored_dict(state)

    unique_key = f"{chat_key}:{msg.id}"

    # 1. Check if this message was marked as an exception!
    if unique_key in ignored_dict:
        return False

    text = msg.raw_text or ""
    matched_tags = find_matching_tags(text, group_cfg["hashtags"])
    if not matched_tags:
        return False

    link = generate_message_link(chat, chat_key, msg.id)
    formatted_body = format_post(matched_tags, group_cfg["title"], text, link)
    msg_buttons = build_message_buttons(link, unique_key)

    # 2. If already sent -> Check if text was edited in the source group
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
                    buttons=msg_buttons,
                    link_preview=False
                )
                saved_entry["text"] = text
                saved_entry["tags"] = matched_tags
                save_json(STATE_FILE, state)
                return True
            except MessageNotModifiedError:
                saved_entry["text"] = text
                save_json(STATE_FILE, state)
            except Exception:
                pass
        return False

    # 3. New message -> Send it to target_chat_id (the shared group!)
    while True:
        try:
            extra_bot_msg_ids = []
            if msg.media:
                async with media_lock:
                    while not bot_media_queue.empty():
                        bot_media_queue.get_nowait()

                    is_forwarding_media = True
                    try:
                        user_fwd = await user_client.forward_messages(bot_username, msg)
                        bot_rx = await asyncio.wait_for(bot_media_queue.get(), timeout=15.0)
                    finally:
                        is_forwarding_media = False

                    try:
                        await bot_client.delete_messages(owner_id, [bot_rx.id])
                    except Exception:
                        pass
                    try:
                        fwd_id = user_fwd.id if hasattr(user_fwd, 'id') else user_fwd[0].id
                        await user_client.delete_messages(bot_username, [fwd_id])
                    except Exception:
                        pass

                if len(formatted_body) <= 1024:
                    sent_msg = await bot_client.send_message(
                        target_chat_id,
                        formatted_body,
                        file=bot_rx.media,
                        buttons=msg_buttons,
                        link_preview=False
                    )
                else:
                    short_header = (
                        f"📌 **الهاشتاغ:** {', '.join(matched_tags)}\n"
                        f"👥 **المجموعة:** {group_cfg['title']}\n"
                        f"📎 *(النص طويل، مرفق في الرسالة التالية 👇)*"
                    )
                    first_media_msg = await bot_client.send_message(
                        target_chat_id,
                        short_header,
                        file=bot_rx.media,
                        buttons=msg_buttons
                    )
                    extra_bot_msg_ids.append(first_media_msg.id)
                    sent_msg = await bot_client.send_message(
                        target_chat_id,
                        formatted_body[:4096],
                        buttons=msg_buttons,
                        link_preview=False
                    )
            else:
                sent_msg = await bot_client.send_message(
                    target_chat_id,
                    formatted_body[:4096],
                    buttons=msg_buttons,
                    link_preview=False
                )

            state["messages"][unique_key] = {
                "bot_msg_id": sent_msg.id,
                "extra_bot_msg_ids": extra_bot_msg_ids,
                "target_chat_id": target_chat_id,
                "text": text,
                "tags": matched_tags,
                "group_title": group_cfg["title"],
                "source_link": link
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
        tags = list(group_cfg.get("hashtags", []))
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
        [Button.inline("📚 اختر مادة", b"subj_years")],
        [Button.inline("📋 المجموعات المراقبة", b"menu_groups"),
         Button.inline("➕ إضافة / تعديل مجموعة", b"menu_add_0")],
        [Button.inline("🗑️ حذف مجموعة", b"menu_remove"),
         Button.inline("🔄 فحص الرسائل القديمة", b"menu_scan")],
        [Button.inline("🚫 الرسائل المستثناة (استعادة)", b"menu_ignored_0")],
        [Button.inline("📍 اعتماد هذه المحادثة للنشر", b"menu_set_here")],
        [Button.inline("♻️ إعادة نسخ الكل من الصفر", b"menu_force_rescan")]
    ]


async def setup_slash_menu():
    commands = [
        BotCommand(command="start", description="🏠 فتح لوحة التحكم الرئيسية بالأزرار"),
        BotCommand(command="subjects", description="📚 اختر مادة حسب السنة الدراسية"),
        BotCommand(command="ignored", description="🚫 عرض واستعادة الرسائل المستثناة"),
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
    ensure_ignored_dict(state)

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

    # 2. Handle commands, media bridging, and text input from ANY member in the group
    @bot_client.on(events.NewMessage)
    async def bot_message_handler(event):
        if event.is_private and event.sender_id == owner_id and is_forwarding_media and (event.message.fwd_from or event.message.media):
            await bot_media_queue.put(event.message)
            return

        sender_id = event.sender_id
        raw_text = (event.raw_text or "").strip()
        cmd = raw_text.split('@')[0].strip() if raw_text.startswith('/') else raw_text

        cfg = load_json(CONFIG_FILE)
        if not event.is_private and "_target_chat_id" not in cfg:
            cfg["_target_chat_id"] = event.chat_id
            save_json(CONFIG_FILE, cfg)

        # Handle interactive text inputs
        if sender_id in pending_user_input and not raw_text.startswith('/'):
            input_state = pending_user_input.pop(sender_id)
            mode = input_state.get("mode")

            if mode == "group_tags":
                chat_key = input_state["id"]
                title = input_state["title"]

                tags = [
                    (t.strip() if t.strip().startswith('#') else f"#{t.strip()}")
                    for t in raw_text.replace('،', ',').split(',') if t.strip()
                ]
                cfg = load_json(CONFIG_FILE)
                cfg[chat_key] = {"title": title, "hashtags": tags}
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

            elif mode == "add_subject_name":
                year_idx = input_state["year_idx"]
                year_name = ACADEMIC_YEARS[year_idx]
                sub_name = raw_text.strip()

                cfg = load_json(CONFIG_FILE)
                tree = get_subjects_tree(cfg)
                if year_name not in tree:
                    tree[year_name] = {}
                if sub_name not in tree[year_name]:
                    tree[year_name][sub_name] = {"lectures": [], "boards": [], "records": []}
                save_json(CONFIG_FILE, cfg)

                sub_list = list(tree[year_name].keys())
                sub_idx = sub_list.index(sub_name)
                await event.reply(
                    f"✅ **تمت إضافة المادة:** `{sub_name}` في **{year_name}**.\n\n"
                    f"الآن اختر القسم الذي تريد تحديد هاشتاغاته لهذه المادة:",
                    buttons=subject_categories_buttons(year_idx, sub_idx, edit_mode=True)
                )
                return

            elif mode == "add_subject_tags":
                year_idx = input_state["year_idx"]
                sub_idx = input_state["sub_idx"]
                cat_key = input_state["cat_key"]

                year_name = ACADEMIC_YEARS[year_idx]
                cfg = load_json(CONFIG_FILE)
                tree = get_subjects_tree(cfg)
                sub_list = list(tree.get(year_name, {}).keys())
                if sub_idx >= len(sub_list):
                    await event.reply("⚠️ حدث خطأ في العثور على المادة.", buttons=main_menu_buttons())
                    return

                sub_name = sub_list[sub_idx]
                tags = [
                    (t.strip() if t.strip().startswith('#') else f"#{t.strip()}")
                    for t in raw_text.replace('،', ',').split(',') if t.strip()
                ]
                tree[year_name][sub_name][cat_key] = tags

                monitored = get_monitored_groups(cfg)
                for g_id, g_info in monitored.items():
                    existing = set(g_info.get("hashtags", []))
                    for t in tags:
                        existing.add(t)
                    g_info["hashtags"] = list(existing)

                save_json(CONFIG_FILE, cfg)
                cat_label = CATEGORY_LABELS.get(cat_key, cat_key)
                await event.reply(
                    f"✅ **تم حفظ هاشتاغات {cat_label} لمادة {sub_name}:**\n"
                    f"`{', '.join(tags)}`\n\n"
                    f"يمكنك الآن إضافة هاشتاغات قسم آخر أو تصفح المادة مباشرة:",
                    buttons=subject_categories_buttons(year_idx, sub_idx, edit_mode=False)
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
                "يمكن لأي عضو في المجموعة التحكم بالبوت أو اختيار مادة محددة من الأزرار أدناه:",
                buttons=main_menu_buttons()
            )
        elif cmd == '/subjects':
            await show_years_menu(event, is_callback=False)
        elif cmd == '/ignored':
            await show_ignored_menu(event, page=0, is_callback=False)
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
            pending_user_input.pop(sender_id, None)
            await event.edit(
                "🏠 **لوحة التحكم الرئيسية:**\nاختر من الأزرار أدناه:",
                buttons=main_menu_buttons()
            )

        # ---------------- SAFE MESSAGE EXCEPTION & RESTORE HANDLERS ----------------
        elif data.startswith("exc_ask|"):
            unique_key = data.split("|", 1)[1]
            confirm_btns = [
                [
                    Button.inline("✅ نعم، احذف واستثنِ", f"exc_yes|{unique_key}".encode('utf-8')),
                    Button.inline("❌ إلغاء", f"exc_no|{unique_key}".encode('utf-8'))
                ]
            ]
            await event.answer("⚠️ هل أنت متأكد من استثناء وحذف هذه الرسالة نهائياً؟", alert=False)
            await event.edit(buttons=confirm_btns)

        elif data.startswith("exc_no|"):
            unique_key = data.split("|", 1)[1]
            saved_entry = state.get("messages", {}).get(unique_key, {})
            link = saved_entry.get("source_link")
            if not link:
                parts = unique_key.split(":")
                clean_id = parts[0].replace('-100', '').replace('-', '')
                link = f"https://t.me/c/{clean_id}/{parts[1]}"
            await event.answer("تم إلغاء الحذف.")
            await event.edit(buttons=build_message_buttons(link, unique_key))

        elif data.startswith("exc_yes|"):
            unique_key = data.split("|", 1)[1]
            ignored_dict = ensure_ignored_dict(state)

            # Pop from synced messages and save rich metadata into ignored_messages
            saved_entry = state.get("messages", {}).pop(unique_key, None)
            snippet = make_snippet(saved_entry.get("text", "")) if saved_entry else f"رسالة ({unique_key})"
            group_title = saved_entry.get("group_title", "مجموعة مراقبة") if saved_entry else "مجموعة مراقبة"
            link = saved_entry.get("source_link", "") if saved_entry else ""

            ignored_dict[unique_key] = {
                "snippet": snippet,
                "group_title": group_title,
                "link": link
            }
            save_json(STATE_FILE, state)

            await event.answer("🚫 تم استثناء الرسالة وحذفها! يمكنك استعادتها من قائمة (الرسائل المستثناة).", alert=True)
            try:
                await event.delete()
                if saved_entry and saved_entry.get("extra_bot_msg_ids"):
                    await bot_client.delete_messages(event.chat_id, saved_entry["extra_bot_msg_ids"])
            except Exception:
                pass

        elif data.startswith("menu_ignored_"):
            page = int(data.split("_")[-1])
            await show_ignored_menu(event, page=page, is_callback=True)

        elif data.startswith("rest_exc|"):
            _, page_str, unique_key = data.split("|", 2)
            page = int(page_str)
            ignored_dict = ensure_ignored_dict(state)

            if unique_key not in ignored_dict:
                await event.answer("⚠️ هذه الرسالة غير موجودة في قائمة الاستثناءات.", alert=True)
                await show_ignored_menu(event, page=page, is_callback=True)
                return

            # 1. Remove from exception list
            restored_meta = ignored_dict.pop(unique_key)
            save_json(STATE_FILE, state)

            # 2. Immediately fetch and re-post the message from the source group
            await event.answer("♻️ جاري استعادة الرسالة وإعادة نشرها...", alert=False)
            restored_ok = False
            try:
                chat_key, msg_id_str = unique_key.split(":")
                msg_id = int(msg_id_str)
                cfg = load_json(CONFIG_FILE)
                monitored = get_monitored_groups(cfg)
                target_chat_id = get_target_chat_id(cfg, event.chat_id)

                if chat_key in monitored:
                    chat_id_int = int(chat_key) if chat_key.lstrip('-').isdigit() else chat_key
                    chat = await user_client.get_entity(chat_id_int)
                    orig_msg = await user_client.get_messages(chat, ids=msg_id)
                    if orig_msg:
                        restored_ok = await sync_single_message(
                            orig_msg, chat, chat_key, monitored[chat_key],
                            target_chat_id, owner_id, state, bot_username
                        )
            except Exception as e:
                print(f"⚠️ Error restoring {unique_key}: {e}")

            if restored_ok:
                await bot_client.send_message(
                    event.chat_id,
                    f"✅ **تمت استعادة الرسالة وإعادة نشرها بنجاح:**\n`{restored_meta.get('snippet', unique_key)}`"
                )
            else:
                await bot_client.send_message(
                    event.chat_id,
                    f"✅ **تمت إزالة الرسالة من قائمة الاستثناءات:** `{restored_meta.get('snippet', unique_key)}`\n"
                    f"*(سيتم جلبها تلقائياً عند الفحص القادم إذا كانت المجموعة لا تزال مراقبة)*"
                )

            await show_ignored_menu(event, page=page, is_callback=True)

        elif data == "clear_all_ignored":
            state["ignored_messages"] = {}
            save_json(STATE_FILE, state)
            await event.answer("♻️ تم تفريغ قائمة الاستثناءات بالكامل! يمكنك الآن الضغط على فحص الرسائل القديمة لجلبها.", alert=True)
            await show_ignored_menu(event, page=0, is_callback=True)

        # ---------------- SUBJECT BROWSER HANDLERS ----------------
        elif data == "subj_years":
            pending_user_input.pop(sender_id, None)
            await show_years_menu(event, is_callback=True)

        elif data.startswith("subj_yr_"):
            year_idx = int(data.split("_")[-1])
            await show_year_subjects_menu(event, year_idx)

        elif data.startswith("subj_add_"):
            year_idx = int(data.split("_")[-1])
            year_name = ACADEMIC_YEARS[year_idx]
            pending_user_input[sender_id] = {"mode": "add_subject_name", "year_idx": year_idx}
            await event.edit(
                f"✍️ **إضافة مادة جديدة في {year_name}:**\n\n"
                f"أرسل الآن اسم المادة في المحادثة (مثال: `كيمياء صيدلية 2`):",
                buttons=[[Button.inline("🔙 إلغاء والعودة", f"subj_yr_{year_idx}".encode('utf-8'))]]
            )

        elif data.startswith("subj_delmenu_"):
            year_idx = int(data.split("_")[-1])
            await show_delete_subject_menu(event, year_idx)

        elif data.startswith("subj_del_"):
            _, _, y_str, s_str = data.split("_")
            year_idx, sub_idx = int(y_str), int(s_str)
            year_name = ACADEMIC_YEARS[year_idx]
            cfg = load_json(CONFIG_FILE)
            tree = get_subjects_tree(cfg)
            sub_list = list(tree.get(year_name, {}).keys())
            if sub_idx < len(sub_list):
                removed_sub = sub_list[sub_idx]
                tree[year_name].pop(removed_sub, None)
                save_json(CONFIG_FILE, cfg)
                await event.answer(f"🗑️ تم حذف مادة {removed_sub}", alert=True)
            await show_year_subjects_menu(event, year_idx)

        elif data.startswith("subj_pick_"):
            _, _, y_str, s_str = data.split("_")
            year_idx, sub_idx = int(y_str), int(s_str)
            await show_subject_categories(event, year_idx, sub_idx, edit_mode=False)

        elif data.startswith("subj_edit_"):
            _, _, y_str, s_str = data.split("_")
            year_idx, sub_idx = int(y_str), int(s_str)
            await show_subject_categories(event, year_idx, sub_idx, edit_mode=True)

        elif data.startswith("subj_setcat_"):
            _, _, y_str, s_str, cat_key = data.split("_")
            year_idx, sub_idx = int(y_str), int(s_str)
            year_name = ACADEMIC_YEARS[year_idx]
            cfg = load_json(CONFIG_FILE)
            tree = get_subjects_tree(cfg)
            sub_name = list(tree[year_name].keys())[sub_idx]
            cat_label = CATEGORY_LABELS.get(cat_key, cat_key)
            current_tags = ", ".join(tree[year_name][sub_name].get(cat_key, []))
            note = f"\n*(الهاشتاغات الحالية: {current_tags})*" if current_tags else ""

            pending_user_input[sender_id] = {
                "mode": "add_subject_tags",
                "year_idx": year_idx,
                "sub_idx": sub_idx,
                "cat_key": cat_key
            }
            await event.edit(
                f"🏷️ **تحديد هاشتاغات {cat_label}**\n"
                f"📚 المادة: **{sub_name}** ({year_name}){note}\n\n"
                f"✍️ أرسل الآن الهاشتاغات الخاصة بهذا القسم مفصولة بفاصلة، مثال:\n"
                f"`#تفريغات_صيدلية_٢, #محاضرات_صيدلية_٢`",
                buttons=[[Button.inline("🔙 إلغاء والعودة", f"subj_pick_{year_idx}_{sub_idx}".encode('utf-8'))]]
            )

        elif data.startswith("subj_get_"):
            _, _, y_str, s_str, cat_key = data.split("_")
            year_idx, sub_idx = int(y_str), int(s_str)
            await fetch_subject_category_results(event, year_idx, sub_idx, cat_key)

        # ---------------- EXISTING GROUP & SCAN HANDLERS ----------------
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

            pending_user_input[sender_id] = {"mode": "group_tags", "id": chat_key, "title": title}
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
            ignored_backup = ensure_ignored_dict(state)
            state["messages"] = {}
            state["ignored_messages"] = ignored_backup
            save_json(STATE_FILE, state)

            cfg = load_json(CONFIG_FILE)
            cfg["_target_chat_id"] = event.chat_id
            save_json(CONFIG_FILE, cfg)
            await event.edit("♻️ تم تصفير الذاكرة (مع الحفاظ على الرسائل المستثناة)! جاري إعادة نسخ جميع المحاضرات...")
            added = await scan_all_groups_history(cfg, event.chat_id, owner_id, state, bot_username)
            await bot_client.send_message(
                event.chat_id,
                f"✅ **اكتمل النسخ الشامل من الصفر!**\nتم جلب `{added}` رسالة ومحاضرة إلى هذه المجموعة.",
                buttons=main_menu_buttons()
            )

    # ---------------- UI HELPER FOR IGNORED MESSAGES (RESTORE MENU) ----------------
    async def show_ignored_menu(event, page=0, is_callback=True):
        ignored_dict = ensure_ignored_dict(state)
        items = list(ignored_dict.items())

        if not items:
            text = (
                "🚫 **قائمة الرسائل المستثناة فارغة حالياً.**\n\n"
                "💡 عندما تضغط على زر `⚙️` أسفل أي محاضرة وتختار **نعم، احذف واستثنِ**، ستظهر هنا لتتمكن من استعادتها في أي وقت إذا حذفتها بالخطأ."
            )
            btns = [[Button.inline("🔙 القائمة الرئيسية", b"menu_home")]]
        else:
            per_page = 6
            start = page * per_page
            end = start + per_page
            current_slice = items[start:end]

            lines = [
                f"🚫 **الرسائل المستثناة ({len(items)} رسالة):**",
                "اضغط على زر **♻️ استعادة** بجانب أي رسالة لإلغاء استثنائها وإعادة نشرها فوراً في المجموعة:\n"
            ]
            btns = []
            for idx, (u_key, meta) in enumerate(current_slice, start + 1):
                snippet = meta.get("snippet", u_key)
                g_title = meta.get("group_title", "")
                link = meta.get("link", "")
                if link:
                    lines.append(f"**{idx}.** [{snippet}]({link}) — *({g_title})*")
                else:
                    lines.append(f"**{idx}.** `{snippet}` — *({g_title})*")

                short_btn_label = snippet[:28] + (".." if len(snippet) > 28 else "")
                btns.append([
                    Button.inline(f"♻️ استعادة #{idx}: {short_btn_label}", f"rest_exc|{page}|{u_key}".encode('utf-8'))
                ])

            nav = []
            if page > 0:
                nav.append(Button.inline("⬅️ السابق", f"menu_ignored_{page - 1}".encode('utf-8')))
            if end < len(items):
                nav.append(Button.inline("التالي ➡️", f"menu_ignored_{page + 1}".encode('utf-8')))
            if nav:
                btns.append(nav)

            btns.append([Button.inline("🗑️ إلغاء استثناء الكل (تفريغ القائمة)", b"clear_all_ignored")])
            btns.append([Button.inline("🔙 القائمة الرئيسية", b"menu_home")])
            text = "\n".join(lines)

        if is_callback:
            await event.edit(text, buttons=btns, link_preview=False)
        else:
            await event.reply(text, buttons=btns, link_preview=False)

    # ---------------- UI HELPER FUNCTIONS FOR SUBJECTS ----------------
    async def show_years_menu(event, is_callback=True):
        btns = []
        for idx, yr_name in enumerate(ACADEMIC_YEARS):
            btns.append([Button.inline(f"🎓 {yr_name}", f"subj_yr_{idx}".encode('utf-8'))])
        btns.append([Button.inline("🔙 القائمة الرئيسية", b"menu_home")])

        text = "📚 **اختر السنة الدراسية:**"
        if is_callback:
            await event.edit(text, buttons=btns)
        else:
            await event.reply(text, buttons=btns)

    async def show_year_subjects_menu(event, year_idx):
        year_name = ACADEMIC_YEARS[year_idx]
        cfg = load_json(CONFIG_FILE)
        tree = get_subjects_tree(cfg)
        subjects_dict = tree.get(year_name, {})

        btns = []
        for s_idx, sub_name in enumerate(subjects_dict.keys()):
            btns.append([Button.inline(f"📖 {sub_name}", f"subj_pick_{year_idx}_{s_idx}".encode('utf-8'))])

        control_row = [Button.inline("➕ إضافة مادة لهذه السنة", f"subj_add_{year_idx}".encode('utf-8'))]
        if subjects_dict:
            control_row.append(Button.inline("🗑️ حذف مادة", f"subj_delmenu_{year_idx}".encode('utf-8')))
        btns.append(control_row)
        btns.append([Button.inline("🔙 اختيار سنة أخرى", b"subj_years"),
                     Button.inline("🏠 الرئيسية", b"menu_home")])

        if not subjects_dict:
            text = f"🎓 **{year_name}**\n⚠️ لا توجد مواد مضافة في هذه السنة حتى الآن.\nاضغط على **➕ إضافة مادة لهذه السنة** لإضافة المواد وربطها بالهاشتاغات."
        else:
            text = f"🎓 **{year_name}**\nاختر المادة المطلوبة لعرض أقسامها (تفريغات، صور لوح، تسجيلات):"

        await event.edit(text, buttons=btns)

    async def show_delete_subject_menu(event, year_idx):
        year_name = ACADEMIC_YEARS[year_idx]
        cfg = load_json(CONFIG_FILE)
        tree = get_subjects_tree(cfg)
        subjects_dict = tree.get(year_name, {})

        btns = []
        for s_idx, sub_name in enumerate(subjects_dict.keys()):
            btns.append([Button.inline(f"❌ حذف {sub_name}", f"subj_del_{year_idx}_{s_idx}".encode('utf-8'))])
        btns.append([Button.inline("🔙 عودة للمواد", f"subj_yr_{year_idx}".encode('utf-8'))])

        await event.edit(f"🗑️ **اختر المادة التي تريد حذفها من {year_name}:**", buttons=btns)

    def subject_categories_buttons(year_idx, sub_idx, edit_mode=False):
        prefix = "subj_setcat" if edit_mode else "subj_get"
        btns = [
            [
                Button.inline("📝 تفريغات", f"{prefix}_{year_idx}_{sub_idx}_lectures".encode('utf-8')),
                Button.inline("🖼️ صور لوح", f"{prefix}_{year_idx}_{sub_idx}_boards".encode('utf-8'))
            ],
            [
                Button.inline("🎙️ تسجيلات", f"{prefix}_{year_idx}_{sub_idx}_records".encode('utf-8')),
            ]
        ]
        if not edit_mode:
            btns[1].append(Button.inline("📦 عرض الكل", f"subj_get_{year_idx}_{sub_idx}_all".encode('utf-8')))
            btns.append([Button.inline("⚙️ تعديل هاشتاغات هذه المادة", f"subj_edit_{year_idx}_{sub_idx}".encode('utf-8'))])
        else:
            btns.append([Button.inline("✅ انتهيت من تعديل الهاشتاغات", f"subj_pick_{year_idx}_{sub_idx}".encode('utf-8'))])

        btns.append([
            Button.inline("🔙 العودة للمواد", f"subj_yr_{year_idx}".encode('utf-8')),
            Button.inline("🏠 الرئيسية", b"menu_home")
        ])
        return btns

    async def show_subject_categories(event, year_idx, sub_idx, edit_mode=False):
        year_name = ACADEMIC_YEARS[year_idx]
        cfg = load_json(CONFIG_FILE)
        tree = get_subjects_tree(cfg)
        sub_list = list(tree.get(year_name, {}).keys())
        if sub_idx >= len(sub_list):
            await show_year_subjects_menu(event, year_idx)
            return

        sub_name = sub_list[sub_idx]
        sub_data = tree[year_name][sub_name]

        lec_tags = ", ".join(sub_data.get("lectures", [])) or "غير محدد"
        brd_tags = ", ".join(sub_data.get("boards", [])) or "غير محدد"
        rec_tags = ", ".join(sub_data.get("records", [])) or "غير محدد"

        if edit_mode:
            text = (
                f"⚙️ **إعداد هاشتاغات مادة: {sub_name}** ({year_name})\n\n"
                f"• **📝 التفريغات:** `{lec_tags}`\n"
                f"• **🖼️ صور اللوح:** `{brd_tags}`\n"
                f"• **🎙️ التسجيلات:** `{rec_tags}`\n\n"
                f"👇 اضغط على القسم الذي تريد تحديد أو تعديل هاشتاغاته:"
            )
        else:
            text = (
                f"📖 **مادة: {sub_name}** ({year_name})\n\n"
                f"• **📝 التفريغات:** `{lec_tags}`\n"
                f"• **🖼️ صور اللوح:** `{brd_tags}`\n"
                f"• **🎙️ التسجيلات:** `{rec_tags}`\n\n"
                f"👇 **اختر القسم المطلوب لجلب جميع رسائله ومحاضراته:**"
            )

        await event.edit(text, buttons=subject_categories_buttons(year_idx, sub_idx, edit_mode=edit_mode))

    async def fetch_subject_category_results(event, year_idx, sub_idx, cat_key):
        year_name = ACADEMIC_YEARS[year_idx]
        cfg = load_json(CONFIG_FILE)
        tree = get_subjects_tree(cfg)
        sub_list = list(tree.get(year_name, {}).keys())
        if sub_idx >= len(sub_list):
            return

        sub_name = sub_list[sub_idx]
        sub_data = tree[year_name][sub_name]

        if cat_key == "all":
            target_tags = (
                sub_data.get("lectures", []) +
                sub_data.get("boards", []) +
                sub_data.get("records", [])
            )
            cat_title = "📦 جميع الأقسام"
        else:
            target_tags = sub_data.get(cat_key, [])
            cat_title = CATEGORY_LABELS.get(cat_key, cat_key)

        if not target_tags:
            await event.answer("⚠️ لم يتم تحديد هاشتاغات لهذا القسم بعد! اضغط على (⚙️ تعديل هاشتاغات هذه المادة) لإضافتها.", alert=True)
            return

        await event.edit(f"⏳ جاري البحث وجمع **{cat_title}** لمادة **{sub_name}**...")

        target_chat_id = get_target_chat_id(cfg, event.chat_id)
        await scan_all_groups_history(cfg, target_chat_id, owner_id, state, bot_username)

        ignored_dict = ensure_ignored_dict(state)
        matched_items = []

        for u_key, info in state.get("messages", {}).items():
            if u_key in ignored_dict:
                continue
            msg_text = info.get("text", "")
            if find_matching_tags(msg_text, target_tags):
                bot_msg_id = info.get("bot_msg_id")
                dest_chat = info.get("target_chat_id", target_chat_id)
                clean_dest = str(dest_chat).replace('-100', '').replace('-', '')
                bot_msg_link = f"https://t.me/c/{clean_dest}/{bot_msg_id}" if bot_msg_id else info.get("source_link", "")

                snippet = make_snippet(msg_text, max_len=65)
                matched_items.append((snippet, bot_msg_link, info.get("source_link", "")))

        back_btns = [
            [Button.inline(f"🔙 العودة لأقسام {sub_name}", f"subj_pick_{year_idx}_{sub_idx}".encode('utf-8'))],
            [Button.inline("📚 اختيار مادة أخرى", b"subj_years"), Button.inline("🏠 الرئيسية", b"menu_home")]
        ]

        if not matched_items:
            await event.edit(
                f"📭 **لم يتم العثور على رسائل مطابقة حالياً!**\n"
                f"📖 المادة: **{sub_name}** ({cat_title})\n"
                f"🏷️ الهاشتاغات المبحوث عنها: `{', '.join(target_tags)}`\n\n"
                f"💡 تأكد من أن المجموعة التي تنشر هذه المادة مضافة في **📋 المجموعات المراقبة**.",
                buttons=back_btns
            )
            return

        report_lines = [
            f"📚 **نتائج البحث لمادة: {sub_name}**",
            f"📂 **القسم:** {cat_title} | **العدد:** `{len(matched_items)}`",
            f"🏷️ **الهاشتاغات:** `{', '.join(target_tags)}`",
            "────────────────────"
        ]
        for idx, (snippet, b_link, s_link) in enumerate(matched_items, 1):
            primary_link = b_link or s_link
            report_lines.append(f"**{idx}.** [{snippet}]({primary_link})")

        full_report = "\n".join(report_lines)
        if len(full_report) > 4000:
            full_report = full_report[:3950] + "\n\n*...(تم اختصار القائمة لطولها)*"

        await event.edit(full_report, buttons=back_btns, link_preview=False)

    # ---------------- EXISTING GROUP VIEWS ----------------
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

    async def startup_catchup_scan():
        await asyncio.sleep(2)
        cfg = load_json(CONFIG_FILE)
        if get_monitored_groups(cfg):
            target_chat_id = get_target_chat_id(cfg, owner_id)
            print("🔄 Running automatic startup catch-up scan...")
            added = await scan_all_groups_history(cfg, target_chat_id, owner_id, state, bot_username)
            print(f"✅ Startup catch-up complete! Synced/updated {added} message(s).")

    asyncio.create_task(startup_catchup_scan())

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )


if __name__ == '__main__':
    asyncio.run(main())
