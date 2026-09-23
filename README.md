# Number-picker Telegram bot (webhook, no DB)

## 1. Create the bot
- Talk to @BotFather on Telegram, `/newbot`, get your `BOT_TOKEN`.
- Add the bot as admin to your channel (`CHANNEL_ID`, e.g. `@mychannel` or the numeric `-100...` id)
  so it's allowed to post there.
- Get your own numeric Telegram id (e.g. via @userinfobot) for `ADMIN_CHAT_ID`.

## 2. Deploy on Render (Web Service, free tier)
1. Push this folder to a GitHub repo.
2. Render dashboard -> New -> Web Service -> connect the repo.
3. Runtime: Python. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Add environment variables:
   - `BOT_TOKEN`
   - `ADMIN_CHAT_ID`
   - `CHANNEL_ID`
   - `TOTAL_NUMBERS` (optional, default 100)
   - `PRICE` (optional, default "100 ETB")
   - `WEBHOOK_SECRET` (optional, random string — adds a hard-to-guess path segment)
6. Deploy. Note the public URL Render gives you, e.g. `https://your-app.onrender.com`.

## 3. Register the webhook with Telegram
Run this once (replace values), from your own machine or the Render shell:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://your-app.onrender.com/webhook/<WEBHOOK_SECRET>"
```

(Drop the `/<WEBHOOK_SECRET>` part of the URL if you didn't set that env var.)

Check it worked:
```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

## Known limitations of this "no database" setup
- **State is in-memory only.** Render's free tier sleeps the service after ~15 minutes
  with no traffic and wipes memory on the next request (cold start) or on redeploy.
  A number reserved as "pending" or a screenshot mid-review can vanish.
- **No persistence across deploys.** Every `git push` that triggers a redeploy resets
  all numbers back to "free".
- **Single instance only** — fine here since Render free tier doesn't scale horizontally anyway.
- If you outgrow this, the lowest-effort upgrade path is either Render's free Postgres
  add-on, or logging orders as rows to a Google Sheet via its API — both keep the "no
  real database to manage" feel while surviving restarts.

## Legal note
Confirm that operating a paid number-selection/lottery scheme is permitted where your
users are before launching — in many countries this requires a gambling/lottery license,
even at small scale and even when payments are verified manually.
