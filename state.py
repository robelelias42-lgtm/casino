"""
In-memory state for the "buy numbers" bot — multi-select version.
Everything here resets when the process restarts (Render free tier sleep/redeploy).

There is ONE live table at a time (table["id"] increments each time admin starts a new one).
"""

table = {
    "id": 0,
    "total": 0,
    "channel_message_id": None,
    "ended": False,
    "winner": None,        # {"number": n, "name": "..."}
    "numbers": {},         # n -> {"status": "free"/"pending"/"sold", "user_id": None, "name": None}
}

user_selection = {}    # user_id -> set of numbers currently selected, not yet submitted for approval
conv_state = {}        # user_id -> "await_phone" / "await_username" / "await_name" / "await_screenshot" / None
draft_info = {}        # user_id -> {"phone": .., "username": .., "name": ..}
pending_approvals = {}  # user_id -> {"numbers": [...], "phone": .., "username": .., "name": .., "photo_file_id": ..}
user_view_msg = {}     # user_id -> (chat_id, message_id) of that user's current table view in their own chat


def new_table(total):
    table["id"] += 1
    table["total"] = total
    table["channel_message_id"] = None
    table["ended"] = False
    table["winner"] = None
    table["numbers"] = {n: {"status": "free", "user_id": None, "name": None} for n in range(1, total + 1)}
    user_selection.clear()
    conv_state.clear()
    draft_info.clear()
    pending_approvals.clear()
    user_view_msg.clear()


def get_selection(user_id):
    return user_selection.setdefault(user_id, set())


def clear_user_progress(user_id):
    user_selection.pop(user_id, None)
    conv_state.pop(user_id, None)
    draft_info.pop(user_id, None)


def try_reserve(numbers, user_id):
    """Attempt to mark each number 'pending' for this user. Returns (added, rejected)
    where rejected is a list of (number, reason) tuples."""
    added, rejected = [], []
    for n in numbers:
        info = table["numbers"].get(n)
        if not info:
            rejected.append((n, "not on the table"))
            continue
        if info["status"] == "sold":
            rejected.append((n, "already sold"))
            continue
        if info["status"] == "pending" and info["user_id"] != user_id:
            rejected.append((n, "requested by someone else"))
            continue
        info["status"] = "pending"
        info["user_id"] = user_id
        get_selection(user_id).add(n)
        added.append(n)
    return added, rejected


def release_numbers(numbers, only_if_owner=None):
    for n in numbers:
        info = table["numbers"].get(n)
        if not info:
            continue
        if only_if_owner is not None and info.get("user_id") != only_if_owner:
            continue
        info["status"] = "free"
        info["user_id"] = None
        info["name"] = None
