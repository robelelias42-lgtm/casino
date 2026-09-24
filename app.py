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
  /endtable 7       -> ends the current table and appends the winner (number 7) in BOLD
                        at the bottom of the channel list.

Everything is in-memory (see state.py) — a Render free-tier restart wipes it.
"""

import os
import logging
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

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

FREE, PENDING, SOLD = "🟢", "🟡", "🔴"

app = Flask(__name__)

# Start with a default table so /start works even before admin runs /newtable.
state.new_table(DEFAULT_TOTAL_NUMBERS)


# ---------- helpers ----------

def esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg(method, payload):
    r = requests.post(f"{API_URL}/{method}", json=payload, timeout=15)
    if not r.ok:
        log.error("Telegram API error on %s: %s", method, r.text)
    return r.json()


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


# ---------- rendering ----------

def render_table_text():
    t = state.table
    lines = [
        f"🎯 <b>Table #{t['id']}</b> — {t['total']} numbers",
        f"💰 Price: {esc(PRICE)} each",
        "",
    ]
    row, rows = [], []
    for n in range(1, t["total"] + 1):
        info = t["numbers"][n]
        if info["status"] == "sold":
            label = f"{SOLD}{n}"
            if info.get("name"):
                label += f"·{esc(info['name'])}"
        elif info["status"] == "pending":
            label = f"{PENDING}{n}"
        else:
            label = f"{FREE}{n}"
        row.append(label)
        if len(row) == 5:
            rows.append(" ".join(row))
            row = []
    if row:
        rows.append(" ".join(row))
    lines.extend(rows)
    lines.append("")
    lines.append(f"{FREE} Free    {PENDING} Requested    {SOLD} Sold")
    if t.get("winner"):
        lines.append("")
        w = t["winner"]
        lines.append(f"🏆 <b>WINNER: Number {w['number']} — {esc(w['name'])}</b> 🏆")
    return "\n".join(lines)


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
        tg("editMessageText", {
            "chat_id": CHANNEL_ID, "message_id": t["channel_message_id"],
            "text": text, "parse_mode": "HTML",
        })
    else:
        r = tg("sendMessage", {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML"})
        t["channel_message_id"] = r.get("result", {}).get("message_id")


def picker_instructions(user_id):
    selected = state.get_selection(user_id)
    if selected:
        return (f"Your picks so far: {', '.join(map(str, sorted(selected)))}\n"
                f"Reply with more numbers to add, or tap Done / Cancel below.")
    return "Reply with the numbers you want, separated by commas (e.g. 3, 17, 42)."


def show_table_to_user(chat_id, user_id):
    text = render_table_text() + "\n\n" + picker_instructions(user_id)
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
    text = render_table_text() + "\n\n" + picker_instructions(user_id)
    kb = render_picker_keyboard(user_id)
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if kb:
        payload["reply_markup"] = kb
    tg("editMessageText", payload)


def finalize_selection(chat_id, user_id):
    selected = state.get_selection(user_id)
    if not selected:
        send_message(chat_id, "Pick at least one number first (reply with numbers like 3, 17, 42), then /done.")
        return
    state.conv_state[user_id] = "await_phone"
    send_message(chat_id, f"You picked: {', '.join(map(str, sorted(selected)))}.\nPlease send your phone number:")


def handle_number_picks(chat_id, user_id, text):
    import re
    candidates = [int(x) for x in re.findall(r"\d+", text)]
    if not candidates:
        send_message(chat_id, "Send /start to see the live table, or reply with numbers like 3, 17, 42 to pick them.")
        return
    added, rejected = state.try_reserve(candidates, user_id)
    update_channel_message()
    refresh_user_view_in_place(user_id)

    parts = []
    if added:
        parts.append(f"Added: {', '.join(map(str, added))}")
    if rejected:
        parts.append("Couldn't add: " + ", ".join(f"{n} ({reason})" for n, reason in rejected))
    parts.append("Reply with more numbers, tap ✅ Done when finished, or ❌ Cancel picks.")
    send_message(chat_id, "\n".join(parts))


def approval_keyboard(user_id):
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{user_id}"},
            {"text": "❌ Reject", "callback_data": f"reject:{user_id}"},
        ]]
    }


# ---------- webhook ----------

@app.route(f"/webhook/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    log.info("update: %s", update)
    if "message" in update:
        handle_message(update["message"])
    elif "callback_query" in update:
        handle_callback(update["callback_query"])
    return jsonify(ok=True)


@app.route("/", methods=["GET"])
def health():
    return "bot is running", 200


# ---------- message handling ----------

def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "")

    # --- admin commands ---
    if is_admin(user_id) and text.startswith("/newtable"):
        parts = text.split()
        total = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else DEFAULT_TOTAL_NUMBERS
        state.new_table(total)
        update_channel_message()
        send_message(chat_id, f"New table #{state.table['id']} started with {total} numbers. Posted to the channel.")
        return

    if is_admin(user_id) and text.startswith("/endtable"):
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            send_message(chat_id, "Usage: /endtable <winning number>, e.g. /endtable 7")
            return
        n = int(parts[1])
        info = state.table["numbers"].get(n)
        if not info or info["status"] != "sold":
            send_message(chat_id, f"Number {n} was never sold, pick a sold number.")
            return
        state.table["ended"] = True
        state.table["winner"] = {"number": n, "name": info.get("name") or "Unknown"}
        update_channel_message()
        send_message(chat_id, f"Table ended. Winner (#{n}) posted to the channel.")
        return

    if text.startswith("/start"):
        state.clear_user_progress(user_id)
        show_table_to_user(chat_id, user_id)
        return

    if text.startswith("/done"):
        finalize_selection(chat_id, user_id)
        return

    if text.startswith("/cancel"):
        selected = state.get_selection(user_id)
        state.release_numbers(selected, only_if_owner=user_id)
        state.clear_user_progress(user_id)
        update_channel_message()
        send_message(chat_id, "Your in-progress picks were released. Send /start to pick again.")
        return

    conv = state.conv_state.get(user_id)

    if conv == "await_phone":
        state.draft_info.setdefault(user_id, {})["phone"] = text.strip()
        state.conv_state[user_id] = "await_username"
        send_message(chat_id, "Got it. Now send your Telegram username (e.g. @yourname):")
        return

    if conv == "await_username":
        state.draft_info.setdefault(user_id, {})["username"] = text.strip()
        state.conv_state[user_id] = "await_name"
        send_message(chat_id, "Now send your full name or nickname:")
        return

    if conv == "await_name":
        state.draft_info.setdefault(user_id, {})["name"] = text.strip()
        state.conv_state[user_id] = "await_screenshot"
        send_message(chat_id, "Last step — send a screenshot of your payment.")
        return

    if "photo" in msg and conv == "await_screenshot":
        file_id = msg["photo"][-1]["file_id"]
        info = state.draft_info.get(user_id, {})
        numbers = sorted(state.get_selection(user_id))
        state.pending_approvals[user_id] = {
            "numbers": numbers,
            "phone": info.get("phone", ""),
            "username": info.get("username", ""),
            "name": info.get("name", ""),
            "photo_file_id": file_id,
        }
        caption = (
            f"Payment screenshot\n"
            f"Numbers: {', '.join(map(str, numbers))}\n"
            f"Name: {info.get('name','')}\n"
            f"Username: {info.get('username','')}\n"
            f"Phone: {info.get('phone','')}\n"
            f"(telegram id {user_id})"
        )
        tg("sendPhoto", {
            "chat_id": ADMIN_CHAT_ID, "photo": file_id, "caption": caption,
            "reply_markup": approval_keyboard(user_id),
        })
        send_message(chat_id, "Screenshot received. Waiting for admin approval.")
        state.conv_state[user_id] = None
        return

    if conv is None and text and not text.startswith("/"):
        handle_number_picks(chat_id, user_id, text)
        return

    send_message(chat_id, "Send /start to see the live table.")


# ---------- callback handling ----------

def handle_callback(cq):
    data = cq["data"]
    callback_id = cq["id"]
    user_id = cq["from"]["id"]
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]

    if data == "cancelpicks":
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

    if data.startswith("approve:") and user_id == ADMIN_CHAT_ID:
        buyer_id = int(data.split(":")[1])
        approval = state.pending_approvals.pop(buyer_id, None)
        if not approval:
            answer_callback(callback_id, "No pending approval found (maybe already handled).")
            return
        for n in approval["numbers"]:
            info = state.table["numbers"].get(n)
            if info:
                info["status"] = "sold"
                info["user_id"] = buyer_id
                info["name"] = approval["name"]
        state.clear_user_progress(buyer_id)
        update_channel_message()
        answer_callback(callback_id, "Approved")
        send_message(buyer_id, f"✅ Approved! Numbers {', '.join(map(str, approval['numbers']))} are now yours.")
        return

    if data.startswith("reject:") and user_id == ADMIN_CHAT_ID:
        buyer_id = int(data.split(":")[1])
        approval = state.pending_approvals.pop(buyer_id, None)
        if not approval:
            answer_callback(callback_id, "No pending approval found (maybe already handled).")
            return
        state.release_numbers(approval["numbers"], only_if_owner=buyer_id)
        state.clear_user_progress(buyer_id)
        update_channel_message()
        answer_callback(callback_id, "Rejected")
        send_message(buyer_id, "❌ Your payment was rejected. Send /start to try again.")
        return

    answer_callback(callback_id)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
