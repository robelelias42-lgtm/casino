"""
Telegram "buy numbers" bot — webhook mode, no database.

USER FLOW
  1. /start -> sees the live table as colored numbers:
        🟢 free   🟡 requested (someone is buying it)   🔴 sold
     Picks numbers by REPLYING WITH TEXT, e.g. "3, 17, 42" (not by tapping each one —
     Telegram inline keyboards cap out around 100 buttons, which breaks at large table sizes).
  2. Tap "✅ Done" -> bot asks for phone number, then telegram username, then name/nickname.
  3. Bot asks for a payment screenshot -> forwarded to the admin with Approve/Reject buttons.
  4. Admin Approve -> numbers become sold, buyer's NAME appears next to them in the channel
     list (phone/username are shown to the admin only, not posted publicly).
     Admin Reject -> numbers go back to free, user is notified.

ADMIN FLOW (admin talks to the bot directly, in a private chat)
  /newtable 50      -> starts a brand-new table with 50 numbers (1..50), posts a fresh
                        list message in the channel, resets everything from the old table.
  /draw             -> (only when EVERY number is sold) runs the live random draw in the
                        channel. Also offered as a button when the table fills up.
                        Set AUTO_DRAW=1 to start it automatically on the last approval.
  /endtable 7       -> manual override: ends the table and appends winner (number 7) in
                        BOLD at the bottom of the channel list. Number must be sold.

Everything is in-memory (see state.py) — a Render free-tier restart wipes it.
Run with ONE gunicorn worker (state lives in this process's memory).
"""

import os
import re
import time
import random
import secrets
import logging
import threading
from flask import Flask, request, jsonify
import requests

import state

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DEFAULT_TOTAL_NUMBERS = int(os.environ.get("TOTAL_NUMBERS", "100"))
PRICE = os.environ.get("PRICE", "100 ETB")
MAX_TABLE_NUMBERS = int(os.environ.get("MAX_TABLE_NUMBERS", "300"))
AUTO_DRAW = os.environ.get("AUTO_DRAW", "0") == "1"

# Live-draw animation tuning (each frame is ONE edit of ONE channel message).
DRAW_FRAMES = int(os.environ.get("DRAW_FRAMES", "12"))
DRAW_DELAY_MIN = float(os.environ.get("DRAW_DELAY_MIN", "0.8"))   # seconds, start of spin
DRAW_DELAY_MAX = float(os.environ.get("DRAW_DELAY_MAX", "1.8"))   # seconds, last frames (slowing down)

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

FREE, PENDING, SOLD = "🟢", "🟡", "🔴"
LINE = "━━━━━━━━━━━━━━━━━━━━"
TG_TEXT_BUDGET = 3900          # Telegram's hard limit is 4096; keep a safety margin
REVIEW_CAPTION_MAX = 900       # photo captions are limited to 1024 chars
MAX_PICK_TOKENS = 200

# (columns per row, max characters of buyer name shown). The first layout that fits in one
# Telegram message is used; big tables degrade gracefully instead of failing to post.
TABLE_LAYOUTS = [(3, 14), (3, 10), (3, 8), (3, 6), (4, 5), (5, 0)]

app = Flask(__name__)

# Start with a default table so /start works even before admin runs /newtable.
state.new_table(DEFAULT_TOTAL_NUMBERS)


# ---------- helpers ----------

def esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def utf16_len(text):
    # Telegram measures message length in UTF-16 code units (emoji count as 2).
    return len(text.encode("utf-16-le")) // 2


def tg(method, payload):
    try:
        r = requests.post(f"{API_URL}/{method}", json=payload, timeout=15)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.error("Telegram API call %s failed: %s", method, e)
        return {"ok": False, "description": str(e)}
    if not data.get("ok"):
        desc = data.get("description", "")
        (log.info if "not modified" in desc else log.error)("Telegram API error on %s: %s", method, data)
    return data


def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return tg("sendMessage", payload)


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    tg("answerCallbackQuery", payload)


def is_admin(user_id):
    return user_id == ADMIN_CHAT_ID


def fmt_numbers(nums):
    return ", ".join(map(str, sorted(nums)))


def compute_amount(count):
    """count x PRICE. PRICE looks like '100 ETB'; if it can't be parsed, show 'N × PRICE'."""
    m = re.match(r"\s*(\d[\d,]*(?:\.\d+)?)\s*(.*)$", PRICE)
    if not m:
        return f"{count} × {PRICE}"
    total = float(m.group(1).replace(",", "")) * count
    shown = f"{int(total):,}" if total == int(total) else f"{total:,.2f}"
    return f"{shown} {m.group(2).strip()}".strip()


# ---------- rendering ----------

def _short(name, limit):
    name = name.strip()
    return name if len(name) <= limit else name[: max(limit - 1, 1)] + "…"


def _build_table_text(cols, name_limit):
    t = state.table
    total = t["total"]
    width = len(str(total))
    c = state.counts()
    sold, pending, free = c["sold"], c["pending"], c["free"]
    pct = sold * 100 // total if total else 0
    filled = sold * 10 // total if total else 0
    bar = "▰" * filled + "▱" * (10 - filled)

    lines = [
        f"🎫 <b>TABLE #{t['id']}</b>  ·  {total} numbers",
        LINE,
        f"💰 Price: <b>{esc(PRICE)}</b> per number",
        f"📊 Sold: <b>{sold}/{total}</b>   {bar} {pct}%",
    ]
    if t.get("ended"):
        lines.append("🏁 <b>TABLE CLOSED</b>")
    elif t.get("drawing"):
        lines.append("🎰 <b>LIVE DRAW IN PROGRESS</b>")
    elif total and sold == total:
        lines.append("✅ <b>ALL NUMBERS SOLD</b> — draw coming up")
    lines.append(LINE)

    sep = "   " if cols <= 3 else "  "
    cells, hidden_names = [], False
    for n in range(1, total + 1):
        info = t["numbers"][n]
        num = f"{n:0{width}d}"
        if info["status"] == "sold":
            cell = f"{SOLD} {num}"
            if info.get("name"):
                if name_limit:
                    cell += f" - {esc(_short(info['name'], name_limit))}"
                else:
                    hidden_names = True
        elif info["status"] == "pending":
            cell = f"{PENDING} {num}"
        else:
            cell = f"{FREE} {num}"
        cells.append(cell)
    lines.extend(sep.join(cells[i:i + cols]) for i in range(0, len(cells), cols))

    lines.append(LINE)
    lines.append(f"{FREE} Free <b>{free}</b>    {PENDING} Requested <b>{pending}</b>    {SOLD} Sold <b>{sold}</b>")
    if hidden_names:
        lines.append("<i>Buyer names hidden: table is too large for one Telegram message.</i>")
    if t.get("winner"):
        w = t["winner"]
        lines.append("")
        lines.append(f"🏆 <b>WINNER: Number {w['number']} — {esc(w['name'])}</b> 🏆")
    return "\n".join(lines)


def render_table_text(reserve=0):
    """Table text that fits in ONE Telegram message. `reserve` = chars the caller will append."""
    budget = TG_TEXT_BUDGET - reserve
    text = ""
    for cols, name_limit in TABLE_LAYOUTS:
        text = _build_table_text(cols, name_limit)
        if utf16_len(text) <= budget:
            break
    return text


def render_picker_keyboard(user_id):
    # Only 2 buttons here (safe at any table size — Telegram caps inline keyboards
    # around 100 buttons total, which is why per-number tap buttons don't scale).
    selected = state.get_selection(user_id)
    if not selected:
        return None
    return {
        "inline_keyboard": [[
            {"text": f"✅ Done ({len(selected)} picked)", "callback_data": "done"},
            {"text": "❌ Cancel picks", "callback_data": "cancelpicks"},
        ]]
    }


def update_channel_message():
    t = state.table
    text = render_table_text()
    if t.get("channel_message_id"):
        r = tg("editMessageText", {
            "chat_id": CHANNEL_ID, "message_id": t["channel_message_id"],
            "text": text, "parse_mode": "HTML",
        })
        desc = (r.get("description") or "").lower()
        if r.get("ok") or "not modified" in desc:
            return
        if "not found" in desc:
            # The live message was deleted from the channel -> post a fresh one below.
            t["channel_message_id"] = None
        else:
            return
    r = tg("sendMessage", {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML"})
    t["channel_message_id"] = r.get("result", {}).get("message_id")


def picker_instructions(user_id):
    selected = state.get_selection(user_id)
    if selected:
        return (
            f"🧾 <b>YOUR SELECTION</b>\n{LINE}\n"
            f"🎫 <b>{fmt_numbers(selected)}</b>\n"
            f"💰 {len(selected)} number(s) · <b>{esc(compute_amount(len(selected)))}</b>\n\n"
            "Reply with more numbers to add, or tap <b>✅ Done</b> / <b>❌ Cancel picks</b> below."
        )
    return (
        f"✍️ <b>HOW TO PICK</b>\n{LINE}\n"
        "Reply with the numbers you want, separated by commas.\n"
        "Example: <code>3, 17, 42</code>"
    )


def compose_user_view(user_id):
    instr = picker_instructions(user_id)
    return render_table_text(reserve=utf16_len(instr) + 8) + "\n\n" + instr


def show_table_to_user(chat_id, user_id):
    text = compose_user_view(user_id)
    kb = render_picker_keyboard(user_id)
    old = state.user_view_msg.get(user_id)
    if old:
        try:
            tg("deleteMessage", {"chat_id": old[0], "message_id": old[1]})
        except Exception:
            pass
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if kb:
        payload["reply_markup"] = kb
    r = tg("sendMessage", payload)
    mid = r.get("result", {}).get("message_id")
    if mid:
        state.user_view_msg[user_id] = (chat_id, mid)


def refresh_user_view_in_place(user_id):
    old = state.user_view_msg.get(user_id)
    if not old:
        return
    chat_id, message_id = old
    text = compose_user_view(user_id)
    kb = render_picker_keyboard(user_id)
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if kb:
        payload["reply_markup"] = kb
    tg("editMessageText", payload)


def finalize_selection(chat_id, user_id):
    if user_id in state.pending_approvals:
        send_message(chat_id, "⏳ Your payment is already waiting for admin review. You'll be notified once it's decided.")
        return
    if state.table.get("ended"):
        send_message(chat_id, "🏁 This table has ended. Wait for the next one.")
        return
    selected = state.get_selection(user_id)
    if not selected:
        send_message(chat_id, "Pick at least one number first (reply with numbers like 3, 17, 42), then /done.")
        return
    state.conv_state[user_id] = "await_phone"
    send_message(
        chat_id,
        f"✅ <b>NUMBERS LOCKED</b>\n{LINE}\n"
        f"🎫 <b>{fmt_numbers(selected)}</b>\n"
        f"💰 Total: <b>{esc(compute_amount(len(selected)))}</b>\n\n"
        "<b>Step 1/3</b> · 📱 Please send your phone number:",
        parse_mode="HTML",
    )


def parse_number_tokens(text):
    """Split free text into valid positive integers and ignored junk tokens.
    'abc', '-5', '3.5', '1e9' are junk; digits-only tokens up to 6 digits are numbers."""
    valid, invalid = [], []
    tokens = [x for x in re.split(r"[\s,;]+", text.strip()) if x]
    for tok in tokens[:MAX_PICK_TOKENS]:
        if re.fullmatch(r"[0-9]{1,6}", tok):
            n = int(tok)
            if n not in valid:
                valid.append(n)
        else:
            invalid.append(esc(tok[:15]))
    return valid, invalid


def handle_number_picks(chat_id, user_id, text):
    if state.table.get("ended"):
        send_message(chat_id, "🏁 This table has ended. Wait for the next one.")
        return
    if user_id in state.pending_approvals:
        send_message(chat_id, "⏳ Your payment is waiting for admin review. You can pick again once it's decided.")
        return

    valid, invalid = parse_number_tokens(text)
    if not valid:
        msg = "⚠️ I couldn't find any valid numbers in that message.\n"
        if invalid:
            msg += f"Ignored: {', '.join(invalid[:10])}\n"
        msg += ("\nReply with numbers separated by commas, e.g. <code>3, 17, 42</code>, "
                "or send /start to see the live table.")
        send_message(chat_id, msg, parse_mode="HTML")
        return

    added, rejected = state.try_reserve(valid, user_id)
    if added:
        update_channel_message()
    refresh_user_view_in_place(user_id)

    parts = []
    if added:
        parts.append(f"✅ Added: <b>{fmt_numbers(added)}</b>")
    if rejected:
        parts.append("⚠️ Couldn't add: " + ", ".join(f"{n} ({reason})" for n, reason in rejected))
    if invalid:
        parts.append(f"🚫 Ignored (not valid numbers): {', '.join(invalid[:10])}")
    parts.append("Reply with more numbers, tap ✅ Done when finished, or ❌ Cancel picks.")
    send_message(chat_id, "\n".join(parts), parse_mode="HTML")


def approval_keyboard(user_id, aid):
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{user_id}:{aid}"},
            {"text": "❌ Reject", "callback_data": f"reject:{user_id}:{aid}"},
        ]]
    }


def render_review_text(buyer_id, approval):
    n = len(approval["numbers"])
    return (
        f"💳 <b>PAYMENT REVIEW</b>\n{LINE}\n"
        f"👤 Name: <b>{esc(approval['name'])}</b>\n"
        f"📱 Phone: <code>{esc(approval['phone'])}</code>\n"
        f"🔗 Username: {esc(approval['username'])}\n"
        f"🆔 Telegram ID: <code>{buyer_id}</code>\n\n"
        f"🎫 Numbers ({n}):\n<b>{fmt_numbers(approval['numbers'])}</b>\n\n"
        f"💰 Amount:\n<b>{esc(compute_amount(n))}</b>\n\n"
        "Please verify the payment screenshot before approving."
    )


def strip_buttons(cq):
    msg = cq["message"]
    tg("editMessageReplyMarkup", {
        "chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
        "reply_markup": {"inline_keyboard": []},
    })


def mark_review(cq, review_html, status_line):
    """Rewrite the admin's review message with the decision and remove the buttons."""
    msg = cq["message"]
    base = {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"], "parse_mode": "HTML"}
    text = f"{review_html}\n\n{status_line}"
    if "photo" in msg:
        tg("editMessageCaption", {**base, "caption": text})
    else:
        tg("editMessageText", {**base, "text": text})


# ---------- payment decisions ----------

def approve_payment(cq, buyer_id, aid):
    callback_id = cq["id"]
    approval = state.pending_approvals.get(buyer_id)
    if not approval or approval.get("aid") != aid:
        strip_buttons(cq)
        answer_callback(callback_id, "Already handled (or from an old table).")
        return

    t = state.table
    stale = [
        n for n in approval["numbers"]
        if not t["numbers"].get(n)
        or t["numbers"][n]["status"] != "pending"
        or t["numbers"][n]["user_id"] != buyer_id
    ]
    state.pending_approvals.pop(buyer_id, None)

    if t.get("ended") or stale:
        # Never sell numbers that are no longer reserved for this buyer (or after the table ended).
        state.release_numbers(approval["numbers"], only_if_owner=buyer_id)
        state.clear_user_progress(buyer_id)
        update_channel_message()
        mark_review(cq, approval["review_html"], "⚠️ <b>NOT APPROVED</b> — numbers were no longer reserved")
        answer_callback(callback_id, "Could not approve: numbers no longer reserved / table ended.")
        send_message(buyer_id, "⚠️ Your order could not be completed because the numbers were no longer reserved. "
                               "Contact the admin about your payment. Send /start to pick again.")
        return

    for n in approval["numbers"]:
        info = t["numbers"][n]
        info["status"] = "sold"
        info["user_id"] = buyer_id
        info["name"] = approval["name"]
    state.clear_user_progress(buyer_id)
    update_channel_message()
    mark_review(cq, approval["review_html"], "✅ <b>APPROVED</b>")
    c = state.counts()
    answer_callback(callback_id, f"Approved ✅  ({c['sold']}/{t['total']} sold)")
    send_message(
        buyer_id,
        f"✅ <b>PAYMENT APPROVED</b>\n{LINE}\n"
        f"🎫 Your numbers: <b>{fmt_numbers(approval['numbers'])}</b>\n\n"
        "They now show as sold under your name in the channel. Good luck! 🍀",
        parse_mode="HTML",
    )
    if c["sold"] == t["total"]:
        on_table_full()


def reject_payment(cq, buyer_id, aid):
    callback_id = cq["id"]
    approval = state.pending_approvals.get(buyer_id)
    if not approval or approval.get("aid") != aid:
        strip_buttons(cq)
        answer_callback(callback_id, "Already handled (or from an old table).")
        return
    state.pending_approvals.pop(buyer_id, None)
    state.release_numbers(approval["numbers"], only_if_owner=buyer_id)
    state.clear_user_progress(buyer_id)
    update_channel_message()
    mark_review(cq, approval["review_html"], "❌ <b>REJECTED</b>")
    answer_callback(callback_id, "Rejected")
    send_message(buyer_id, "❌ <b>PAYMENT REJECTED</b>\nYour numbers were released. Send /start to try again.",
                 parse_mode="HTML")


# ---------- live draw ----------

def on_table_full():
    t = state.table
    if AUTO_DRAW:
        ok, message = begin_draw()
        send_message(ADMIN_CHAT_ID, "🎰 Table complete — live draw started automatically." if ok else f"⚠️ {message}")
        return
    send_message(
        ADMIN_CHAT_ID,
        f"🎯 <b>TABLE COMPLETE</b>\n{LINE}\n"
        f"All <b>{t['total']}</b> numbers of Table #{t['id']} are sold.\n"
        "Start the live draw when you're ready (or send /draw).",
        reply_markup={"inline_keyboard": [[
            {"text": "🎰 Start live draw", "callback_data": f"draw:{t['id']}"}
        ]]},
        parse_mode="HTML",
    )


def begin_draw():
    """Validate and start the draw. Returns (started, message). Caller holds state.lock."""
    t = state.table
    if t.get("ended") or t.get("winner"):
        return False, "This table has already ended — it can't be drawn again."
    if t.get("drawing"):
        return False, "A draw is already running."
    sold = state.sold_numbers()
    if not t["total"] or len(sold) < t["total"]:
        return False, (f"Not ready: {len(sold)}/{t['total']} numbers sold. "
                       "The draw only runs when every number is sold.")
    if t.get("draw_pick") is None:
        t["draw_pick"] = secrets.choice(sold)   # the real, cryptographically-secure random pick
    t["drawing"] = True
    threading.Thread(target=run_draw, args=(t["id"], sold, t["draw_pick"]), daemon=True).start()
    return True, "Draw started."


def build_reel(sold, pick, frames, window=5):
    """Cosmetic scrolling reel. The centre of the LAST window is the (already drawn) winner."""
    others = [n for n in sold if n != pick] or [pick]
    reel = []
    for _ in range(frames + window - 1):
        n = random.choice(others)
        for _try in range(8):
            if len(others) > 1 and reel and n == reel[-1]:
                n = random.choice(others)
        reel.append(n)
    reel[frames + window // 2 - 1] = pick
    return reel


def render_draw_intro(table_id, count):
    return (
        f"🎰 <b>DRAW STARTING…</b>\n{LINE}\n"
        f"Table #{table_id} · <b>{count}</b> sold numbers in the drum\n\n"
        "🎲 One number will be drawn at random."
    )


def render_draw_frame(table_id, width, window, phase):
    mid = len(window) // 2
    rows = []
    for i, n in enumerate(window):
        label = f"{n:0{width}d}"
        rows.append(f"► {label} ◄" if i == mid else f"  {label}")
    status = {"spin": "🔄 Spinning…", "slow": "⏳ Slowing down…", "last": "🎯 And the number is…"}[phase]
    return (
        f"🎰 <b>LIVE DRAW</b> · Table #{table_id}\n{LINE}\n"
        f"<pre>{chr(10).join(rows)}</pre>\n{status}"
    )


def render_winner_text(table_id, total, number, name):
    return (
        f"🏆 <b>WINNER</b>\n{LINE}\n"
        f"Table #{table_id} · {total} numbers\n\n"
        f"🎫 Number: <b>{number}</b>\n"
        f"👤 Name: <b>{esc(name)}</b>\n\n"
        "🎉 Congratulations!"
    )


def edit_draw_message(message_id, text, final=False):
    """Edit the draw message. Respects Telegram flood-control (429 retry_after)."""
    for _ in range(5 if final else 2):
        r = tg("editMessageText", {"chat_id": CHANNEL_ID, "message_id": message_id,
                                   "text": text, "parse_mode": "HTML"})
        if r.get("ok") or "not modified" in (r.get("description") or "").lower():
            return True
        retry = (r.get("parameters") or {}).get("retry_after")
        if retry is not None:
            if not final and retry > 3:
                return False          # skip this cosmetic frame instead of stalling the animation
            time.sleep(min(retry, 10) + 0.2)
            continue
        if not final:
            return False
        time.sleep(1)
    return False


def run_draw(table_id, sold, pick):
    """Runs in a background thread so the webhook request returns immediately."""
    try:
        with state.lock:
            if state.table["id"] != table_id:
                return
            total = state.table["total"]
        width = len(str(total))

        r = tg("sendMessage", {"chat_id": CHANNEL_ID, "text": render_draw_intro(table_id, len(sold)),
                               "parse_mode": "HTML"})
        mid = r.get("result", {}).get("message_id") if r.get("ok") else None
        with state.lock:
            if state.table["id"] == table_id:
                update_channel_message()      # table header now says "LIVE DRAW IN PROGRESS"
        time.sleep(2)

        if mid:
            frames = max(DRAW_FRAMES, 3)
            reel = build_reel(sold, pick, frames)
            for i in range(frames):
                progress = i / (frames - 1)
                phase = "last" if i == frames - 1 else ("slow" if progress > 0.6 else "spin")
                edit_draw_message(mid, render_draw_frame(table_id, width, reel[i:i + 5], phase))
                if i < frames - 1:
                    time.sleep(DRAW_DELAY_MIN + (DRAW_DELAY_MAX - DRAW_DELAY_MIN) * progress ** 2)
            time.sleep(1.5)

        with state.lock:
            if state.table["id"] != table_id:
                return
            info = state.table["numbers"][pick]
            name = info.get("name") or "Unknown"
            buyer_id = info.get("user_id")

        text = render_winner_text(table_id, total, pick, name)
        if not (mid and edit_draw_message(mid, text, final=True)):
            tg("sendMessage", {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML"})

        with state.lock:
            if state.table["id"] == table_id:
                state.table["winner"] = {"number": pick, "name": name}
                state.table["ended"] = True
                update_channel_message()      # winner line appended to the live table

        send_message(ADMIN_CHAT_ID, f"🏆 Draw complete — winner: number {pick} ({name}).")
        if buyer_id:
            send_message(buyer_id, f"🎉 <b>YOU WON!</b>\nYour number <b>{pick}</b> was drawn in Table #{table_id}. "
                                   "Congratulations!", parse_mode="HTML")
    except Exception:
        log.exception("draw failed")
        try:
            send_message(ADMIN_CHAT_ID, "⚠️ The draw hit an error before finishing. Send /draw to run it again "
                                        "(the already-drawn number is kept).")
        except Exception:
            pass
    finally:
        with state.lock:
            if state.table["id"] == table_id:
                state.table["drawing"] = False


# ---------- webhook ----------

@app.route(f"/webhook/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    log.info("update: %s", update)
    try:
        with state.lock:
            if "message" in update:
                handle_message(update["message"])
            elif "callback_query" in update:
                handle_callback(update["callback_query"])
    except Exception:
        # Always answer 200, otherwise Telegram re-delivers the same failing update forever.
        log.exception("error while handling update")
    return jsonify(ok=True)


@app.route("/", methods=["GET"])
def health():
    return "bot is running", 200


# ---------- message handling ----------

def clean_line(text):
    return " ".join((text or "").split())


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "") or ""

    # --- admin commands ---
    if is_admin(user_id) and text.startswith("/newtable"):
        if state.table.get("drawing"):
            send_message(chat_id, "🎰 A draw is running right now — wait for it to finish before starting a new table.")
            return
        parts = text.split()
        if len(parts) > 1:
            if not re.fullmatch(r"[0-9]{1,6}", parts[1]) or not (2 <= int(parts[1]) <= MAX_TABLE_NUMBERS):
                send_message(chat_id, f"Usage: /newtable <count>  (between 2 and {MAX_TABLE_NUMBERS}), e.g. /newtable 100")
                return
            total = int(parts[1])
        else:
            total = DEFAULT_TOTAL_NUMBERS
        state.new_table(total)
        update_channel_message()
        send_message(chat_id, f"New table #{state.table['id']} started with {total} numbers. Posted to the channel.")
        return

    if is_admin(user_id) and text.startswith("/endtable"):
        t = state.table
        if t.get("drawing"):
            send_message(chat_id, "🎰 A draw is running right now — wait for it to finish.")
            return
        if t.get("ended"):
            send_message(chat_id, "This table has already ended. Start a new one with /newtable <count>.")
            return
        parts = text.split()
        if len(parts) < 2 or not re.fullmatch(r"[0-9]{1,6}", parts[1]):
            send_message(chat_id, "Usage: /endtable <winning number>, e.g. /endtable 7")
            return
        n = int(parts[1])
        info = t["numbers"].get(n)
        if not info or info["status"] != "sold":
            send_message(chat_id, f"Number {n} was never sold, pick a sold number.")
            return
        t["ended"] = True
        t["winner"] = {"number": n, "name": info.get("name") or "Unknown"}
        update_channel_message()
        send_message(chat_id, f"Table ended. Winner (#{n}) posted to the channel.")
        return

    if is_admin(user_id) and text.startswith("/draw"):
        ok, message = begin_draw()
        send_message(chat_id, "🎰 Live draw started in the channel." if ok else f"⚠️ {message}")
        return

    if text.startswith("/start"):
        state.clear_user_progress(user_id)
        show_table_to_user(chat_id, user_id)
        return

    if text.startswith("/done"):
        finalize_selection(chat_id, user_id)
        return

    if text.startswith("/cancel"):
        if user_id in state.pending_approvals:
            send_message(chat_id, "⏳ Your payment is already with the admin for review, so it can't be cancelled now.")
            return
        selected = state.get_selection(user_id)
        state.release_numbers(selected, only_if_owner=user_id)
        state.clear_user_progress(user_id)
        update_channel_message()
        send_message(chat_id, "Your in-progress picks were released. Send /start to pick again.")
        return

    conv = state.conv_state.get(user_id)

    if conv == "await_phone":
        phone = clean_line(text)
        digits = re.sub(r"\D", "", phone)
        if not (7 <= len(digits) <= 15) or len(phone) > 25 or text.startswith("/"):
            send_message(chat_id, "⚠️ That doesn't look like a phone number. Please send it as text, e.g. 09XXXXXXXX:")
            return
        state.draft_info.setdefault(user_id, {})["phone"] = phone
        state.conv_state[user_id] = "await_username"
        send_message(chat_id, "<b>Step 2/3</b> · 🔗 Got it. Now send your Telegram username (e.g. @yourname):",
                     parse_mode="HTML")
        return

    if conv == "await_username":
        username = clean_line(text)
        if not username or len(username) > 64 or text.startswith("/"):
            send_message(chat_id, "⚠️ Please send your Telegram username as text (e.g. @yourname):")
            return
        if re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
            username = "@" + username
        state.draft_info.setdefault(user_id, {})["username"] = username
        state.conv_state[user_id] = "await_name"
        send_message(chat_id, "<b>Step 3/3</b> · 👤 Now send your full name or nickname "
                              "(this is shown publicly next to your numbers):", parse_mode="HTML")
        return

    if conv == "await_name":
        name = clean_line(text)
        if not name or len(name) > 30 or text.startswith("/"):
            send_message(chat_id, "⚠️ Please send a name or nickname of up to 30 characters:")
            return
        state.draft_info.setdefault(user_id, {})["name"] = name
        state.conv_state[user_id] = "await_screenshot"
        send_message(chat_id, "💳 Last step — send a screenshot of your payment.")
        return

    if conv == "await_screenshot" and "photo" not in msg:
        send_message(chat_id, "📸 Please send the payment screenshot as a photo (not as a file).")
        return

    if "photo" in msg and conv == "await_screenshot":
        file_id = msg["photo"][-1]["file_id"]
        info = state.draft_info.get(user_id, {})
        numbers = sorted(state.get_selection(user_id))
        aid = state.next_approval_id()
        approval = {
            "aid": aid,
            "numbers": numbers,
            "phone": info.get("phone", ""),
            "username": info.get("username", ""),
            "name": info.get("name", ""),
            "photo_file_id": file_id,
        }
        review = render_review_text(user_id, approval)
        approval["review_html"] = review
        kb = approval_keyboard(user_id, aid)
        if utf16_len(review) <= REVIEW_CAPTION_MAX:
            r = tg("sendPhoto", {"chat_id": ADMIN_CHAT_ID, "photo": file_id, "caption": review,
                                 "parse_mode": "HTML", "reply_markup": kb})
        else:
            # Long number lists don't fit in a photo caption: photo first, then details + buttons.
            r = tg("sendPhoto", {"chat_id": ADMIN_CHAT_ID, "photo": file_id,
                                 "caption": "💳 <b>PAYMENT SCREENSHOT</b> — details below", "parse_mode": "HTML"})
            if r.get("ok"):
                r = send_message(ADMIN_CHAT_ID, review, reply_markup=kb, parse_mode="HTML")
        if not r.get("ok"):
            send_message(chat_id, "⚠️ I couldn't deliver your screenshot to the admin. Please send it again.")
            return
        state.pending_approvals[user_id] = approval
        send_message(chat_id, f"📨 <b>SCREENSHOT RECEIVED</b>\n{LINE}\n"
                              "Waiting for admin approval — you'll be notified here.", parse_mode="HTML")
        state.conv_state[user_id] = None
        return

    if conv is None and text and not text.startswith("/"):
        handle_number_picks(chat_id, user_id, text)
        return

    send_message(chat_id, "Send /start to see the live table.")


# ---------- callback handling ----------

def handle_callback(cq):
    data = cq.get("data", "")
    callback_id = cq["id"]
    user_id = cq["from"]["id"]
    chat_id = cq["message"]["chat"]["id"]

    if data == "cancelpicks":
        if user_id in state.pending_approvals:
            answer_callback(callback_id, "Your payment is under review — can't cancel now.")
            return
        selected = state.get_selection(user_id)
        state.release_numbers(selected, only_if_owner=user_id)
        state.clear_user_progress(user_id)
        update_channel_message()
        refresh_user_view_in_place(user_id)
        answer_callback(callback_id, "Picks cancelled.")
        return

    if data == "done":
        if not state.get_selection(user_id):
            answer_callback(callback_id, "Pick at least one number first.")
            return
        answer_callback(callback_id, "Great — now send your details.")
        finalize_selection(chat_id, user_id)
        return

    if user_id == ADMIN_CHAT_ID and data.startswith(("approve:", "reject:")):
        parts = data.split(":")
        try:
            buyer_id = int(parts[1])
        except (ValueError, IndexError):
            answer_callback(callback_id, "Invalid button.")
            return
        aid = parts[2] if len(parts) > 2 else None
        if parts[0] == "approve":
            approve_payment(cq, buyer_id, aid)
        else:
            reject_payment(cq, buyer_id, aid)
        return

    if user_id == ADMIN_CHAT_ID and data.startswith("draw:"):
        if data.split(":")[1] != str(state.table["id"]):
            strip_buttons(cq)
            answer_callback(callback_id, "Old button — that table is no longer active.")
            return
        ok, message = begin_draw()
        answer_callback(callback_id, "🎰 Draw started!" if ok else message[:190])
        if ok:
            strip_buttons(cq)
        return

    answer_callback(callback_id)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
