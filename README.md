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
   - `PRICE` (optional, default "100 ETB")
   - `WEBHOOK_SECRET` (optional, any string you make up — adds a hard-to-guess path segment)
6. Deploy. Note the public URL Render gives you, e.g. `https://your-app.onrender.com`.

   Note: `TOTAL_NUMBERS` is no longer an env var — table size is now set live via the
   `/newtable` admin command below, so you can change it between rounds without redeploying.

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

## Admin commands (send these directly to the bot from your ADMIN_CHAT_ID account)
- `/newtable 50` — starts a brand-new table with numbers 1–50, wipes the previous round's
  state, and posts a fresh list message to the channel.
- `/endgame 7` — marks number 7 (must already be "sold") as the winner and appends it in
  bold to the channel's list message.

## User flow
1. `/start` shows the live table as a grid of buttons: 🟩 free, 🟨 requesting (pending admin
   review), 🟥 sold. Tapping a free number adds/removes it from their cart (multi-select).
2. "✅ Done" reserves everything in their cart (turns those numbers 🟨 for everyone) and the
   bot asks, one at a time: phone number → Telegram username → name/nickname.
3. After that, the bot asks for a payment screenshot. Once sent, it's forwarded to you (the
   admin) with Approve/Reject buttons and the user's submitted info.
4. Approve → those numbers turn 🟥 sold, and the channel list is updated with the user's
   name/username (phone is kept private to your admin chat, not posted publicly).
   Reject → numbers go back to 🟩 free.

Every time the bot sends a fresh `/start` table view to a user, it deletes their previous
table-view message first, so their chat doesn't fill up with old copies of the table.

## How to use it (admin)
Message the bot **directly, in a private chat** (not the channel) as the admin account:
- `/newtable 50` — starts a brand new table with numbers 1–50, posts a fresh list to the
  channel, and wipes any old table's picks/sales. Use this each time you start a new round.
- `/endtable 7` — ends the current table and adds "🏆 WINNER: Number 7 — <name>" in bold
  at the bottom of the channel list. Number 7 must already be marked sold.
- When a user sends a payment screenshot, you'll get it as a direct message with
  ✅ Approve / ❌ Reject buttons. Approving marks their numbers sold and updates the channel;
  their phone number and Telegram username are shown to you there but are **not** posted
  publicly — only their name/nickname appears in the channel.

## How it works (user)
1. `/start` — shows the live table with colored numbers: 🟢 free, 🟡 requested, 🔴 sold.
2. Tap any green number to select it (tap again to deselect) — multiple numbers can be
   picked at once. The same message updates in place, so the chat doesn't fill up with
   repeated tables.
3. Tap "✅ Done", then answer the phone number / username / name prompts.
4. Send a screenshot of the payment — it goes to the admin for approval.
5. `/cancel` — if a user wants to back out before sending a screenshot, this releases
   their in-progress picks back to "free".

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

## About "color combo to build trust"
Telegram bots can't change button/app colors — that's controlled by each user's own
Telegram theme, not the bot. The 🟢🟡🔴 emoji status system above is the practical
equivalent inside chat. For visual branding beyond that, set a profile photo and
description for the bot via @BotFather (`/setuserpic`, `/setdescription`).

## Legal note
Confirm that operating a paid number-selection/lottery scheme is permitted where your
users are before launching — in many countries this requires a gambling/lottery license,
even at small scale and even when payments are verified manually.
