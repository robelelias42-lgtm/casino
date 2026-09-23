"""
Pure in-memory state. Everything here is lost when the process restarts
(e.g. Render free tier spinning down after 15 min idle, or a redeploy).

number_status[n] one of: "free", "pending", "taken"
number_owner[n]  -> user_id once pending/taken
pending_screenshots[user_id] -> (number, file_id)
"""

number_status = {}
number_owner = {}
pending_screenshots = {}


def _ensure_init(total):
    if not number_status:
        for n in range(1, total + 1):
            number_status[n] = "free"


def get_available_numbers(total):
    _ensure_init(total)
    return [n for n, s in number_status.items() if s == "free"]


def reserve_number(number, user_id):
    if number_status.get(number) != "free":
        return False
    number_status[number] = "pending"
    number_owner[number] = user_id
    return True


def release_number(number):
    number_status[number] = "free"
    number_owner.pop(number, None)


def confirm_number(number, user_id):
    number_status[number] = "taken"
    number_owner[number] = user_id


def set_pending_screenshot(user_id, number, file_id):
    pending_screenshots[user_id] = (number, file_id)


def pop_pending_screenshot(user_id):
    return pending_screenshots.pop(user_id, None)


def get_user_pending_number(user_id):
    entry = pending_screenshots.get(user_id)
    if entry:
        return entry[0]
    # also check if they reserved a number but haven't sent a screenshot yet
    for n, owner in number_owner.items():
        if owner == user_id and number_status.get(n) == "pending":
            return n
    return None
