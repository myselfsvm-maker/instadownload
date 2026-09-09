InstaSave Bot

A Telegram bot that fetches public Instagram posts, reels, and IGTV videos (including carousels) and sends the media back in chat.

⚠️ Before you deploy this

This works by using Instagram's private/internal endpoints (via instaloader) — there's no official public API for this kind of thing. That means:

It's against Instagram's Terms of Service, and Instagram actively tries to detect and block this kind of automated access. Expect occasional 429/rate-limit errors, and be prepared for it to break if Instagram changes its internal API.
Only works on public posts — private accounts will fail by design.
Only download/redistribute content you have the right to use. Copyright in the underlying media stays with its creator regardless of what this bot lets you fetch.
Running it at any real scale (many users, always-on) increases the chance Instagram's IP-based rate limiting blocks your Render instance entirely.

If you want something more robust for production use at scale, using Instagram's official Graph API (which requires the content owner's authorization) is the sanctioned path — it just doesn't let you fetch arbitrary public posts the way this bot does.

How it works
python-telegram-bot v21 (async) handles the Telegram side, running in webhook mode so it works on Render's free web-service tier.
instaloader fetches the post's media anonymously (no Instagram login needed for public posts).
An in-memory sliding-window rate limiter throttles requests per chat (resets on restart — fine for this scale, no DB needed).
Downloaded files go to a temp directory and are deleted immediately after sending, so nothing persists on disk.
1. Create the bot on Telegram
Message @BotFather → /newbot → follow prompts.
Copy the token it gives you (looks like 123456789:AA...).
2. Push this repo to GitHub
bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
3. Deploy on Render
render.com → New + → Blueprint → connect your GitHub repo. Render will read render.yaml automatically. (Alternatively: New + → Web Service, runtime Python, build command pip install -r requirements.txt, start command python bot.py.)
When prompted for env vars, set BOT_TOKEN (from BotFather). You don't need to set WEBHOOK_BASE_URL — the bot reads Render's own RENDER_EXTERNAL_URL env var (set automatically on every Render service) and uses that for the webhook, so it comes up in webhook mode on the very first deploy.
Deploy and wait for it to go live.
4. Test it

Open your bot in Telegram, send /start, then paste a public Instagram post or reel link.

Notes on Render's free tier
Free web services spin down after ~15 minutes of inactivity and take a few seconds to wake back up on the next incoming request (Telegram's webhook call). The first message after idle time will feel slow — this is normal on the free tier, not a bug.
No persistent disk is guaranteed across deploys/restarts on the free tier, which is why this bot is intentionally stateless (temp files only, in-memory rate limiting).
Local development
bash
cp .env.example .env   # fill in BOT_TOKEN
pip install -r requirements.txt
export $(cat .env | xargs)  # or use python-dotenv
python bot.py            # runs in polling mode when WEBHOOK_BASE_URL is unset
Extending
More platforms (TikTok, YouTube Shorts, etc.): add a matching URL regex and a fetch function per platform, dispatch based on which pattern matches in handle_message. Each platform has its own scraping quirks/libraries.
Persistent stats / dedupe cache: swap the in-memory rate limiter for SQLite or Redis — straightforward since is_rate_limited is the only touchpoint.
Stories: requires an authenticated instaloader session (login as a real account), which is a materially bigger maintenance and ban-risk surface than public-post fetching — not included here by default.
