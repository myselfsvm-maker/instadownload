"""
InstaSave Bot - Telegram bot that downloads public Instagram posts, reels,
IGTV and carousels and sends the media back to the user.
 
Deployment target: Render.com free web service (webhook mode).
"""
 
import logging
import os
import re
import time
import shutil
import tempfile
from collections import defaultdict, deque
from pathlib import Path
 
import instaloader
from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
 
# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
 
BOT_TOKEN = os.environ["BOT_TOKEN"]  # required, no default on purpose
PORT = int(os.environ.get("PORT", 8080))
# RENDER_EXTERNAL_URL is injected automatically by Render on every service —
# using it means no manual "copy URL, paste back as env var" step is needed.
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", BOT_TOKEN.split(":")[-1])
USE_WEBHOOK = bool(WEBHOOK_BASE_URL)
 
MAX_REQUESTS_PER_WINDOW = int(os.environ.get("RATE_LIMIT_COUNT", 8))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW", 60))
MAX_DOWNLOAD_MB = int(os.environ.get("MAX_DOWNLOAD_MB", 45))  # Telegram bot API cap is 50MB
 
INSTAGRAM_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/"
    r"(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
 
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("instasave-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
 
# --------------------------------------------------------------------------
# Simple in-memory rate limiter (per chat). Resets on restart, which is
# fine for this use case and avoids needing a database on the free tier.
# --------------------------------------------------------------------------
 
_request_log: dict[int, deque] = defaultdict(deque)
 
 
def is_rate_limited(chat_id: int) -> bool:
    now = time.time()
    q = _request_log[chat_id]
    while q and now - q[0] > RATE_LIMIT_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= MAX_REQUESTS_PER_WINDOW:
        return True
    q.append(now)
    return False
 
 
# --------------------------------------------------------------------------
# Instagram fetching
# --------------------------------------------------------------------------
 
_loader = instaloader.Instaloader(
    quiet=True,
    download_pictures=True,
    download_videos=True,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    post_metadata_txt_pattern="",
    max_connection_attempts=2,
)
 
 
class FetchError(Exception):
    pass
 
 
def extract_shortcode(text: str) -> str | None:
    match = INSTAGRAM_URL_RE.search(text)
    return match.group(1) if match else None
 
 
def fetch_post_files(shortcode: str, dest_dir: Path) -> tuple[list[Path], str]:
    """Download all media for a shortcode into dest_dir.
 
    Returns (list_of_file_paths, caption).
    Raises FetchError with a user-friendly message on failure.
    """
    try:
        post = instaloader.Post.from_shortcode(_loader.context, shortcode)
    except instaloader.exceptions.ConnectionException as e:
        logger.error("Instagram connection error for shortcode %s: %s", shortcode, e)
        raise FetchError("Instagram is rate-limiting requests right now. Try again shortly.") from e
    except Exception as e:
        logger.error("Post lookup failed for shortcode %s: %s", shortcode, e)
        raise FetchError("Couldn't find that post. It may be private, deleted, or the link is wrong.") from e
 
    if post.is_video and post.typename != "GraphSidecar":
        pass  # single video, handled by download below
 
    try:
        _loader.dirname_pattern = str(dest_dir)
        _loader.download_post(post, target=str(dest_dir))
    except Exception as e:
        logger.error("Download failed for shortcode %s: %s", shortcode, e)
        raise FetchError("Failed to download the media for that post.") from e
 
    media_files = sorted(
        p for p in dest_dir.iterdir()
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".mp4")
    )
    if not media_files:
        raise FetchError("No downloadable media found in that post.")
 
    caption = (post.caption or "").strip()
    return media_files, caption
 
 
def cleanup(dest_dir: Path) -> None:
    shutil.rmtree(dest_dir, ignore_errors=True)
 
 
# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
 
WELCOME = (
    "👋 *InstaSave Bot*\n\n"
    "Send me a public Instagram link (post, reel, or IGTV) and I'll send "
    "the photo(s) or video(s) right back to you.\n\n"
    "Just paste a link like:\n"
    "`https://www.instagram.com/reel/XXXXXXXXX/`"
)
 
HELP = (
    "*How to use*\n"
    "1. Copy a public Instagram post/reel/IGTV link\n"
    "2. Send it to me here\n"
    "3. I'll reply with the media\n\n"
    "*Notes*\n"
    "• Only public posts work — private accounts can't be fetched\n"
    "• Carousels (multiple photos/videos) are sent as an album\n"
    f"• Rate limit: {MAX_REQUESTS_PER_WINDOW} links per {RATE_LIMIT_WINDOW_SECONDS}s per chat"
)
 
 
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)
 
 
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)
 
 
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
 
    chat_id = message.chat_id
    shortcode = extract_shortcode(message.text)
 
    if not shortcode:
        await message.reply_text(
            "That doesn't look like an Instagram post/reel link. Send me a URL like "
            "`https://www.instagram.com/p/XXXXXXXXX/`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
 
    if is_rate_limited(chat_id):
        await message.reply_text(
            f"⏳ You're sending links too fast. Max {MAX_REQUESTS_PER_WINDOW} per "
            f"{RATE_LIMIT_WINDOW_SECONDS}s — try again in a moment."
        )
        return
 
    status_msg = await message.reply_text("⬇️ Fetching your media...")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
 
    dest_dir = Path(tempfile.mkdtemp(prefix=f"ig_{shortcode}_"))
    try:
        files, caption = fetch_post_files(shortcode, dest_dir)
        await send_media(context, chat_id, files, caption)
        await status_msg.delete()
    except FetchError as e:
        await status_msg.edit_text(f"❌ {e}")
    except Exception:
        logger.exception("Unexpected error handling shortcode %s", shortcode)
        await status_msg.edit_text("❌ Something went wrong fetching that post. Please try again.")
    finally:
        cleanup(dest_dir)
 
 
async def send_media(context: ContextTypes.DEFAULT_TYPE, chat_id: int, files: list[Path], caption: str):
    trimmed_caption = (caption[:900] + "…") if len(caption) > 900 else caption
 
    # Filter out files over the size cap to avoid Telegram upload failures
    safe_files = []
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        if size_mb <= MAX_DOWNLOAD_MB:
            safe_files.append(f)
        else:
            logger.warning("Skipping %s (%.1f MB over cap)", f.name, size_mb)
 
    if not safe_files:
        await context.bot.send_message(chat_id, "❌ The media file is too large to send via Telegram.")
        return
 
    if len(safe_files) == 1:
        f = safe_files[0]
        if f.suffix.lower() == ".mp4":
            with open(f, "rb") as fh:
                await context.bot.send_video(chat_id, fh, caption=trimmed_caption or None)
        else:
            with open(f, "rb") as fh:
                await context.bot.send_photo(chat_id, fh, caption=trimmed_caption or None)
        return
 
    # Carousel -> media group (Telegram allows up to 10 per group)
    for batch_start in range(0, len(safe_files), 10):
        batch = safe_files[batch_start:batch_start + 10]
        media_group = []
        open_handles = []
        for i, f in enumerate(batch):
            fh = open(f, "rb")
            open_handles.append(fh)
            cap = trimmed_caption if (batch_start == 0 and i == 0) else None
            if f.suffix.lower() == ".mp4":
                media_group.append(InputMediaVideo(fh, caption=cap))
            else:
                media_group.append(InputMediaPhoto(fh, caption=cap))
        try:
            await context.bot.send_media_group(chat_id, media=media_group)
        finally:
            for fh in open_handles:
                fh.close()
 
 
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception", exc_info=context.error)
 
 
# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
 
def build_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    return application
 
 
def main():
    app = build_app()
 
    if USE_WEBHOOK:
        url_path = WEBHOOK_SECRET
        webhook_url = f"{WEBHOOK_BASE_URL.rstrip('/')}/{url_path}"
        logger.info("Starting in webhook mode: %s", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=url_path,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        logger.info("WEBHOOK_BASE_URL not set — starting in polling mode (dev only).")
        app.run_polling(drop_pending_updates=True)
 
 
if __name__ == "__main__":
    main()
