"""
Telegram "buy a number" bot — webhook mode, no database.

Flow:
  1. User runs /start -> sees a table of numbers (inline buttons) that are NOT taken.
  2. User taps a number -> bot marks it "pending" and asks them to send a payment screenshot.
  3. User sends a photo -> bot forwards it to the ADMIN_CHAT_ID with Approve/Reject buttons.
  4. Admin taps Approve -> number is confirmed, bot posts the winner/owner to CHANNEL_ID,
     and tells the user their number is confirmed.
     Admin taps Reject -> number is released back into the pool, user is notified.

State lives in a plain Python dict (see `state.py`). This means:
  - It resets to empty every time the Render free service restarts / redeploys / cold-starts
    after sleeping. A payment stuck "pending" when that happens is lost.
  - Only ONE process/instance can run (no horizontal scaling), which is fine on free tier anyway.

Deploy on Render as a "Web Service" (NOT a background worker/cron), because Telegram needs
an HTTPS URL to POST updates to.
"""

import os
import logging
from flask import Flask, request, jsonify
import requests

from state import (
    get_available_numbers,
    reserve_number,
    release_number,
    confirm_number,
    set_pending_screenshot,
    pop_pending_screenshot,
    get_user_pending_number,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]                     # set in Render env vars
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])        # your personal telegram numeric id
CHANNEL_ID = os.environ["CHANNEL_ID"]                   # e.g. "@your_channel" or -100123456789
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")   # optional shared-secret path guard
TOTAL_NUMBERS = int(os.environ.get("TOTAL_NUMBERS", "100"))
PRICE = os.environ.get("PRICE", "100 ETB")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)


# ---------- small helpers to talk to Telegram ----------

def tg(method, payload):
    r = requests.post(f"{API_URL}/{method}", json=payload, timeout=15)
    if not r.ok:
        log.error("Telegram API error on %s: %s", method, r.text)
    return r.json()


def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", payload)


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    tg("answerCallbackQuery", payload)


def numbers_keyboard():
    available = get_available_numbers(TOTAL_NUMBERS)
    row, rows = [], []
    for n in available:
        row.append({"text": str(n), "callback_data": f"pick:{n}"})
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows} if rows else None


def approval_keyboard(user_id, number):
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{user_id}:{number}"},
            {"text": "❌ Reject", "callback_data": f"reject:{user_id}:{number}"},
        ]]
    }


# ---------- webhook endpoint ----------

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
    # Render free tier will hit this to check the service is alive.
    return "bot is running", 200


# ---------- message + callback logic ----------

def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    username = msg["from"].get("username") or msg["from"].get("first_name", "user")

    if "text" in msg and msg["text"].startswith("/start"):
        kb = numbers_keyboard()
        if kb is None:
            send_message(chat_id, "Sorry, all numbers are currently taken.")
        else:
            send_message(
                chat_id,
                f"Pick a number below to buy it for {PRICE}.\n"
                f"After picking, send a screenshot of your payment for approval.",
                reply_markup=kb,
            )
        return

    if "photo" in msg:
        pending_number = get_user_pending_number(user_id)
        if pending_number is None:
            send_message(chat_id, "Pick a number first with /start before sending a screenshot.")
            return

        file_id = msg["photo"][-1]["file_id"]  # largest size
        set_pending_screenshot(user_id, pending_number, file_id)

        # Forward the screenshot to the admin with approve/reject buttons
        tg("sendPhoto", {
            "chat_id": ADMIN_CHAT_ID,
            "photo": file_id,
            "caption": f"Payment screenshot\nUser: @{username} (id {user_id})\nNumber: {pending_number}",
            "reply_markup": approval_keyboard(user_id, pending_number),
        })
        send_message(chat_id, "Screenshot received. Waiting for admin approval.")
        return

    # fallback
    send_message(chat_id, "Send /start to see available numbers.")


def handle_callback(cq):
    data = cq["data"]
    callback_id = cq["id"]
    from_user = cq["from"]["id"]
    username = cq["from"].get("username") or cq["from"].get("first_name", "user")

    if data.startswith("pick:"):
        number = int(data.split(":")[1])
        ok = reserve_number(number, from_user)
        if ok:
            answer_callback(callback_id, f"You picked #{number}")
            send_message(from_user, f"You picked #{number} ({PRICE}). Now send a screenshot of your payment.")
        else:
            answer_callback(callback_id, "That number was just taken, pick another.")
        return

    if data.startswith("approve:") and from_user == ADMIN_CHAT_ID:
        _, user_id, number = data.split(":")
        user_id, number = int(user_id), int(number)
        confirm_number(number, user_id)
        pop_pending_screenshot(user_id)
        answer_callback(callback_id, "Approved")
        send_message(user_id, f"✅ Your payment for number {number} was approved!")
        tg("sendMessage", {
            "chat_id": CHANNEL_ID,
            "text": f"🎉 Number {number} has been claimed!",
        })
        return

    if data.startswith("reject:") and from_user == ADMIN_CHAT_ID:
        _, user_id, number = data.split(":")
        user_id, number = int(user_id), int(number)
        release_number(number)
        pop_pending_screenshot(user_id)
        answer_callback(callback_id, "Rejected")
        send_message(user_id, f"❌ Your payment for number {number} was rejected. Please try again.")
        return

    answer_callback(callback_id)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
