import os, re, asyncio, aiohttp, logging, helper
from pyrogram import Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from config import ext_logs

BASE_URL = "https://backend.multistreaming.site/api"
USER_ID_FOR_ACTIVE = "1448640"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ---------- Global Cache for Pagination ----------
_sw_batches_cache = {}      # chat_id -> {batch_id: batch_info}
_sw_batches_list_cache = {} # chat_id -> list of all batches

# ---------- Async HTTP Helpers ----------
async def fetch_json(session, url):
    try:
        async with session.get(url, headers=HEADERS, timeout=20) as resp:
            return await resp.json()
    except Exception as e:
        logging.error(f"fetch_json error {url}: {e}")
        return None

async def get_active_batches(session):
    url = f"{BASE_URL}/courses?userId={USER_ID_FOR_ACTIVE}"
    data = await fetch_json(session, url)
    if data and isinstance(data, dict):
        if data.get("state") == 200 and isinstance(data.get("data"), list):
            return data["data"]
        elif "data" in data and isinstance(data["data"], list):
            return data["data"]
    return []

async def get_course_classes(session, course_id):
    url = f"{BASE_URL}/courses/{course_id}/classes?populate=full"
    data = await fetch_json(session, url)
    if data and isinstance(data, dict):
        if data.get("state") == 200 and isinstance(data.get("data"), list):
            return data["data"]
        elif "data" in data:
            inner = data["data"]
            if isinstance(inner, dict) and "classes" in inner:
                return inner["classes"]
            elif isinstance(inner, list):
                return inner
    return []

# ---------- Parsing Logic ----------
def extract_subject_from_title(title, fallback="Course"):
    try:
        if "||" in title:
            parts = [p.strip() for p in title.split("||")]
            if len(parts) > 1:
                second = parts[1]
                if "|" in second:
                    return second.split("|")[0].strip()
                return second.strip()
        if "|" in title:
            parts = [p.strip() for p in title.split("|")]
            for p in parts:
                if p and not re.search(r"(?i)class[\s-]*\d+", p):
                    return p
        return fallback
    except:
        return fallback

def normalize_video_entries(class_item):
    title = class_item.get("title") or class_item.get("classTitle") or class_item.get("name") or class_item.get("heading") or "Untitled"
    candidate_links = []

    # Direct keys
    for k in ("class_link", "videoLink", "video_link", "video_url", "videoUrl", "link", "url", "playbackUrl", "playback_url", "streamUrl", "stream_url"):
        v = class_item.get(k)
        if isinstance(v, str) and v: candidate_links.append(v)

    # m3u8 keys
    for k in ("masterPlaylist", "master_playlist", "hlsLink", "hls_link", "secureLink", "secure_link", "m3u8", "m3u8Url", "m3u8_url", "playlist", "playlistUrl"):
        v = class_item.get(k)
        if isinstance(v, str) and v: candidate_links.append(v)

    # Arrays
    for k in ("rawSources", "sources", "recordings", "files", "videoFiles", "videos", "assets"):
        arr = class_item.get(k)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, str) and it: candidate_links.append(it)
                elif isinstance(it, dict):
                    for subk in ("url", "file", "src", "mp4", "m3u8"):
                        vv = it.get(subk)
                        if isinstance(vv, str): candidate_links.append(vv)

    # Nested objects
    for nk in ("playback", "video", "stream", "media"):
        obj = class_item.get(nk)
        if isinstance(obj, dict):
            for subk in ("url", "file", "m3u8", "mp4", "hls", "src"):
                vv = obj.get(subk)
                if isinstance(vv, str): candidate_links.append(vv)
        elif isinstance(obj, list):
            for it in obj:
                if isinstance(it, str): candidate_links.append(it)
                elif isinstance(it, dict):
                    for subk in ("url", "file", "src", "mp4", "m3u8"):
                        vv = it.get(subk)
                        if isinstance(vv, str): candidate_links.append(vv)

    # Embed / iframe
    for k in ("embed", "iframe", "embedHtml"):
        v = class_item.get(k)
        if isinstance(v, str) and "http" in v:
            m = re.search(r"https?://[^\s'\"<>]+", v)
            if m: candidate_links.append(m.group(0))

    # Dedupe
    seen = set()
    clean_candidates = []
    for u in candidate_links:
        if not isinstance(u, str) or not u.strip(): continue
        u = u.strip()
        if u not in seen:
            seen.add(u)
            clean_candidates.append(u)

    hls_links = [u for u in clean_candidates if "m3u8" in u or "playlist-mpl" in u or "hls" in u.lower()]
    other_links = [u for u in clean_candidates if u not in hls_links]

    mp4_list = []
    for u in clean_candidates:
        if u.lower().endswith(".mp4") or ".mp4?" in u.lower():
            mp4_list.append(u)

    explicit_mp4 = class_item.get("mp4Recordings") or class_item.get("mp4_recordings") or class_item.get("mp4records")
    if isinstance(explicit_mp4, list):
        for it in explicit_mp4:
            if isinstance(it, str) and it.strip(): mp4_list.append(it.strip())
            elif isinstance(it, dict):
                for subk in ("url", "file", "mp4"):
                    vv = it.get(subk)
                    if isinstance(vv, str) and vv.strip(): mp4_list.append(vv.strip())
    mp4_list = list(dict.fromkeys(mp4_list))

    # PDFs
    class_pdfs = []
    for key in ("classPdf", "class_pdf", "pdfs", "materials", "resources", "files"):
        arr = class_item.get(key)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, str) and ".pdf" in it.lower(): class_pdfs.append(it.strip())
                elif isinstance(it, dict):
                    for subk in ("url", "file", "pdf"):
                        vv = it.get(subk)
                        if isinstance(vv, str) and ".pdf" in vv.lower(): class_pdfs.append(vv.strip())
    for k in ("pdf", "pdfUrl", "pdf_url", "file"):
        v = class_item.get(k)
        if isinstance(v, str) and ".pdf" in v.lower(): class_pdfs.append(v.strip())
    class_pdfs = list(dict.fromkeys(class_pdfs))

    primary_link = ""
    if hls_links: primary_link = hls_links[0]
    elif other_links: primary_link = other_links[0]

    include_mp4s = False if primary_link and ("m3u8" in primary_link or "hls" in primary_link.lower()) else True

    return {
        "title": title,
        "class_link": primary_link,
        "mp4Recordings": mp4_list if include_mp4s else [],
        "classPdf": class_pdfs
    }

async def find_course_pdf(session, course_id):
    url = f"{BASE_URL}/courses/{course_id}"
    data = await fetch_json(session, url)
    if data and isinstance(data, dict) and data.get("state") == 200:
        course_data = data.get("data", {})
        pdf = course_data.get("batchInfoPdfUrl") or course_data.get("batch_info_pdf") or course_data.get("pdf")
        if pdf:
            if isinstance(pdf, str): return [pdf.strip()]
            elif isinstance(pdf, list): return [p for p in pdf if p]
    return []

async def build_txt_for_course(session, course_id, course_title):
    classes = await get_course_classes(session, course_id)
    if not classes:
        return None, "Failed to fetch classes."

    items_to_process = []
    if classes and isinstance(classes[0], dict) and "topicName" in classes[0] and "classes" in classes[0]:
        for topic_block in classes:
            items_to_process.extend(topic_block.get("classes", []))
    else:
        items_to_process = classes

    lines = []
    total_videos = total_mp4 = total_m3u8 = total_youtube = total_pdfs = 0

    for cls in items_to_process:
        norm = normalize_video_entries(cls)
        title = norm["title"]
        subject = extract_subject_from_title(title, fallback=course_title or "Course")

        if norm["class_link"]:
            lines.append(f"[{subject}] {title} : {norm['class_link']}")
            total_videos += 1
            u = norm["class_link"].lower()
            if "m3u8" in u or "playlist" in u or "hls" in u:
                total_m3u8 += 1
            elif "youtube" in u or "youtu.be" in u:
                total_youtube += 1
            else:
                total_mp4 += 1
        elif norm["mp4Recordings"]:
            for mp4 in norm["mp4Recordings"]:
                lines.append(f"[{subject}] {title} : {mp4}")
                total_videos += 1
                total_mp4 += 1

        for pdf in norm["classPdf"]:
            lines.append(f"[{subject}] {title} : {pdf}")
            total_pdfs += 1

    # Course-level PDFs
    course_pdfs = await find_course_pdf(session, course_id)
    if course_pdfs:
        subj = course_title or "Course"
        for p in course_pdfs:
            lines.append(f"[{subj}] {subj} : {p}")
            total_pdfs += 1

    txt_content = "\n".join(lines)
    summary = {
        "total_links": len(lines),
        "total_videos": total_videos,
        "total_mp4": total_mp4,
        "total_m3u8": total_m3u8,
        "total_youtube": total_youtube,
        "total_pdfs": total_pdfs,
    }
    return txt_content, summary

# ---------- Pagination UI ----------
PAGE_SIZE = 10

def build_batches_keyboard(batches, page=0):
    total = len(batches)
    total_pages = (total - 1) // PAGE_SIZE + 1
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_batches = batches[start:end]

    text_lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        " *<a href=https://whatsapp.com/channel/0029VbBJXE2L2AU8KIbzm52g>𓍯𝙤𝙜𝙗𝙝𝙖𝙩𝙞𝙬𝙖𝙡⚝</a>* ",
        " *Select your batch from below:* ",
        "━━━━━━━━━━━━━━━━━━━━━━\n"
    ]
    for i, b in enumerate(page_batches, start=start + 1):
        title = b.get("title") or b.get("name") or "Untitled"
        bid = b.get("id") or b.get("_id") or ""
        text_lines.append(f"📌 {i}. {title}")
        text_lines.append(f"   🆔 Batch ID: `{bid}`")
        text_lines.append("────────────────────────")

    text_lines.append(f"\n📄 Page {page + 1} of {total_pages}")
    text_lines.append("✨ Send the *Batch ID* to continue.")
    text_lines.append("💡 Tip: Copy the Batch ID above to avoid mistakes!")
    text_lines.append("━━━━━━━━━━━━━━━━━━━")
    text = "\n".join(text_lines)

    buttons = []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"sw_page:{page-1}"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"sw_page:{page+1}"))
    if row:
        buttons.append(row)

    return text, InlineKeyboardMarkup(buttons) if buttons else None

# ---------- Main Handler ----------
async def selection_way_handler(bot: Client, msg: Message, user_id: int):
    chat_id = msg.chat.id
    editable = await msg.reply_text("🔍 Fetching available batches...")

    async with aiohttp.ClientSession() as session:
        batches = await get_active_batches(session)
        if not batches:
            await editable.edit("❌ Failed to fetch batch list. Try again later.")
            return

        _sw_batches_cache[chat_id] = {str(b.get("id") or b.get("_id")): b for b in batches}
        _sw_batches_list_cache[chat_id] = batches

        text, markup = build_batches_keyboard(batches, page=0)
        await editable.edit(text, reply_markup=markup, disable_web_page_preview=True)

        try:
            input_msg = await bot.ask(chat_id, "📥 Please send the Batch ID from the list above.", timeout=120)
            batch_id = input_msg.text.strip()
            await input_msg.delete()
        except asyncio.TimeoutError:
            await editable.edit("⏰ Timeout. Please try again.")
            return

        selected = _sw_batches_cache.get(chat_id, {}).get(batch_id)
        if not selected:
            await editable.edit(f"❌ Invalid Batch ID: `{batch_id}`. Make sure it's exact.")
            return

        course_title = selected.get("title") or selected.get("name") or "Course"
        await editable.edit(f"⏳ Fetching course data for **{course_title}**...")

        txt_content, summary = await build_txt_for_course(session, batch_id, course_title)
        if txt_content is None:
            await editable.edit(f"❌ Failed to fetch course data: {summary}")
            return

        safe_title = re.sub(r"[^\w\s-]", "", course_title).strip().replace(" ", "_")
        filename = f"{safe_title}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(txt_content)

        user = await bot.get_users(chat_id)
        caption = (
            f"<blockquote><b>📱 App Name: Selection Way</b></blockquote>\n\n"
            f"<b>======= BATCH DETAILS =======</b>\n"
            f"<blockquote expandable>🪪 <b>Batch ID :</b> {batch_id}\n"
            f"🌟 <b>Batch Name :</b> {course_title}</blockquote>\n\n"
            f"<b>======= LINK SUMMARY =======</b>\n"
            f"<blockquote expandable>🔢 <b>Total Number of Links :</b> {summary['total_links']}\n"
            f"┠🎥 <b>Total Videos :</b> {summary['total_videos']}\n"
            f"  ┠📺 <b>MP4 Videos :</b> {summary['total_mp4']}\n"
            f"  ┠📡 <b>M3U8 Videos :</b> {summary['total_m3u8']}\n"
            f"  ┠📹 <b>YouTube Videos :</b> {summary['total_youtube']}\n"
            f"┠📄 <b>Total PDFs :</b> {summary['total_pdfs']}</blockquote>\n\n"
            f"🧑‍🏫 <b>Generated By :</b> {user.mention}\n"
            f"📅 <b>Generated On :</b> {helper.get_current_time_ist()}"
        )

        thumb_path = "Extthumb.jpg"

        await bot.send_document(chat_id, filename, caption=caption, thumb=thumb_path)
        await bot.send_document(ext_logs, filename, caption=caption, thumb=thumb_path)

        await editable.delete()
        os.remove(filename)

    _sw_batches_cache.pop(chat_id, None)
    _sw_batches_list_cache.pop(chat_id, None)

# ---------- Pagination Callback ----------
async def handle_selection_way_pagination(bot: Client, callback_query):
    chat_id = callback_query.message.chat.id
    page = int(callback_query.data.split(":")[1])
    batches = _sw_batches_list_cache.get(chat_id, [])
    if not batches:
        await callback_query.answer("Session expired. Please /start again.", show_alert=True)
        return
    text, markup = build_batches_keyboard(batches, page)
    await callback_query.message.edit(text, reply_markup=markup, disable_web_page_preview=True)
    await callback_query.answer()
