"""eDimension deadline reminder -> Telegram.

Every morning it sends a numbered list of the eDimension deadlines due in the
next REMIND_DAYS days. Reply to the bot to tick deadlines off:

    deadline 1 done        strike out deadline 1 (also: "deadline 1 3 done")
    undo                   bring back the last deadline(s) you marked done

Deadlines marked done never appear in later lists. Replies are processed on
each scheduled run (07:47, 08:47 and 09:47 Singapore time), not instantly.

Usage:
    python reminder.py                 # process replies, then send today's list
    python reminder.py --once-per-day  # same, but skip the list if already sent today (07:47 + 09:47 runs)
    python reminder.py --replies-only  # only process replies (08:47 run)
    python reminder.py --preview       # list upcoming events, send nothing
    python reminder.py --dry-run       # print today's list instead of sending it
    python reminder.py --test          # send a test message to Telegram
    python reminder.py --get-chat-id   # print the chat ID of whoever messaged your bot

Settings come from environment variables (or a local .env file):
    ICS_URL             eDimension calendar link (required)
    TELEGRAM_BOT_TOKEN  from @BotFather (required to send)
    TELEGRAM_CHAT_ID    your chat ID (required to send; only this chat can control the bot)
    REMIND_DAYS         how many days ahead to list (default 3)
    TIMEZONE            default Asia/Singapore
    EXCLUDE_PATTERN     optional regex; events whose title matches are skipped
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "state.json"
LEGACY_STATE_FILE = ROOT / "state" / "sent.json"  # from the remind-once version
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = 4000  # Telegram's limit is 4096
HEADER = "⏰ <b>Upcoming eDimension deadlines</b>\n\n"
HELP = (
    "I understand:\n"
    "• <code>deadline 1 done</code> to tick off deadline 1 from the latest list\n"
    "• <code>deadline 1 3 done</code> to tick off several\n"
    "• <code>undo</code> to bring back the last one you ticked off\n\n"
    "I check for replies around 7:47, 8:47 and 9:47am, so I may take a while to answer."
)
DONE_RE = re.compile(r"^\s*deadlines?\s+([\d\s,&and]+?)\s+(?:is\s+|are\s+)?done\W*$", re.I)
UNDO_RE = re.compile(r"^\s*undo\W*$", re.I)
DRY_RUN = False  # when True, messages are printed instead of sent


def load_dotenv(path: Path) -> None:
    """Tiny .env loader so local runs don't need extra setup."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- deadlines


@dataclass
class Deadline:
    uid: str
    title: str
    due: datetime  # timezone-aware
    all_day: bool

    def to_dict(self) -> dict:
        return {"uid": self.uid, "title": self.title, "due": self.due.isoformat(), "all_day": self.all_day}

    @classmethod
    def from_dict(cls, d: dict) -> "Deadline":
        return cls(d["uid"], d["title"], datetime.fromisoformat(d["due"]), d["all_day"])


def to_aware(value: date | datetime, tz: ZoneInfo) -> tuple[datetime, bool]:
    """Normalise an iCal DTSTART value to an aware datetime in `tz`."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(tz), False
    # All-day event: treat as due at the end of that day.
    return datetime.combine(value, time(23, 59), tzinfo=tz), True


def fetch_calendar(url: str) -> Calendar:
    # Blackboard sometimes hands out webcal:// links; they are plain https.
    url = re.sub(r"^webcal://", "https://", url.strip(), flags=re.I)
    resp = requests.get(url, timeout=30, headers={"User-Agent": "deadline-reminder/1.0"})
    resp.raise_for_status()
    if b"BEGIN:VCALENDAR" not in resp.content[:2000]:
        raise ValueError(
            "The calendar link did not return calendar data. It may have expired "
            "or be the wrong link. Get a fresh 'external calendar link' from eDimension."
        )
    return Calendar.from_ical(resp.content)


def parse_deadlines(cal: Calendar, tz: ZoneInfo) -> list[Deadline]:
    deadlines = []
    for event in cal.walk("VEVENT"):
        start = event.get("DTSTART")
        if start is None:
            continue
        # Classes and lectures have a room; deadlines never do.
        if str(event.get("LOCATION", "")).strip():
            continue
        due, all_day = to_aware(start.dt, tz)
        title = str(event.get("SUMMARY", "(untitled)")).strip()
        uid = str(event.get("UID", title))
        deadlines.append(Deadline(uid=uid, title=title, due=due, all_day=all_day))
    deadlines.sort(key=lambda d: d.due)
    return deadlines


# -------------------------------------------------------------------- state
#
# state.json holds:
#   done       {uid: due}      deadlines you ticked off (hidden from future lists)
#   undo       [[uid, ...]]    each "done" command, newest last, for "undo"
#   last_list  [deadline]      the numbered list in the latest reminder
#   offset     int             last Telegram update already processed


def load_state() -> dict:
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    state.setdefault("done", {})
    state.setdefault("undo", [])
    state.setdefault("last_list", [])
    state.setdefault("offset", 0)
    return state


def save_state(state: dict, now: datetime) -> None:
    # Forget ticked-off deadlines more than 30 days in the past.
    cutoff = now - timedelta(days=30)
    state["done"] = {u: d for u, d in state["done"].items() if datetime.fromisoformat(d) >= cutoff}
    state["undo"] = [[u for u in group if u in state["done"]] for group in state["undo"]][-20:]
    state["undo"] = [g for g in state["undo"] if g]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    if LEGACY_STATE_FILE.exists():
        LEGACY_STATE_FILE.unlink()


# --------------------------------------------------------------- formatting


def humanise(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    days, hours = divmod(hours, 24)
    if days and hours:
        return f"{days}d {hours}h"
    if days:
        return f"{days}d"
    if hours:
        return f"{hours}h"
    return "under 1h"


def format_item(n: int, d: Deadline, now: datetime, done: bool = False) -> str:
    when = d.due.strftime("%a %d %b") if d.all_day else d.due.strftime("%a %d %b, %I:%M %p")
    left = d.due - now
    status = f"in {humanise(left)}" if left.total_seconds() > 0 else "passed"
    text = f"{n}. <b>{html.escape(d.title)}</b>\n     Due {when} ({status})"
    return f"<s>{text}</s>" if done else text


def build_messages(items: list[tuple[Deadline, bool]], now: datetime) -> list[str]:
    """Numbered list (1, 2, 3, ...) split across messages if it gets too long."""
    messages, current = [], HEADER
    for n, (d, done) in enumerate(items, start=1):
        block = format_item(n, d, now, done) + "\n\n"
        if len(current) + len(block) > MAX_MESSAGE_LEN:
            messages.append(current.rstrip())
            current = HEADER
        current += block
    messages.append(current.rstrip())
    return messages


def render_last_list(state: dict, now: datetime) -> list[str]:
    items = [(Deadline.from_dict(d), d["uid"] in state["done"]) for d in state["last_list"]]
    return build_messages(items, now)


# ----------------------------------------------------------------- telegram


def telegram(method: str, **params) -> dict:
    token = require_env("TELEGRAM_BOT_TOKEN")
    resp = requests.post(TELEGRAM_API.format(token=token, method=method), json=params, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        # Don't echo the URL: it contains the bot token.
        raise RuntimeError(f"Telegram {method} failed: {data.get('description', resp.status_code)}")
    return data


def send(text: str, reply_to: int | None = None) -> None:
    if DRY_RUN:
        print(text + "\n")
        return
    params = {
        "chat_id": require_env("TELEGRAM_CHAT_ID"),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    telegram("sendMessage", **params)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing setting {name}. Add it to .env (local) or GitHub secrets.")
    return value


def cmd_get_chat_id() -> None:
    updates = telegram("getUpdates").get("result", [])
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("first_name") or chat.get("title")
    if not chats:
        print("No messages found. Open your bot in Telegram, press Start / send it 'hi', then run this again.")
        return
    for chat_id, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={chat_id}   ({name})")


# ------------------------------------------------------------------ replies


def handle_command(text: str, state: dict, now: datetime) -> list[str]:
    """Apply one message from you to the state; return the reply message(s)."""
    last = state["last_list"]

    m = DONE_RE.match(text)
    if m:
        numbers = sorted({int(n) for n in re.findall(r"\d+", m.group(1))})
        bad = [n for n in numbers if not 1 <= n <= len(last)]
        if not last:
            return ["There's no deadline list to tick off yet."]
        if bad:
            return [f"There's no deadline {', '.join(map(str, bad))} in the latest list (it has 1–{len(last)})."]
        newly_done = []
        for n in numbers:
            d = last[n - 1]
            if d["uid"] not in state["done"]:
                state["done"][d["uid"]] = d["due"]
                newly_done.append(d["uid"])
        if newly_done:
            state["undo"].append(newly_done)
        return render_last_list(state, now)

    if UNDO_RE.match(text):
        if not state["undo"]:
            return ["Nothing to undo."]
        for uid in state["undo"].pop():
            state["done"].pop(uid, None)
        return render_last_list(state, now) if last else ["Undone."]

    return [HELP]


def process_replies(state: dict, now: datetime) -> int:
    """Read messages sent to the bot since the last run and answer them."""
    my_chat = str(require_env("TELEGRAM_CHAT_ID"))
    params = {"timeout": 0, "allowed_updates": ["message"]}
    if state["offset"]:
        params["offset"] = state["offset"] + 1  # also tells Telegram to drop older ones
    updates = telegram("getUpdates", **params).get("result", [])

    handled = 0
    for u in sorted(updates, key=lambda u: u["update_id"]):
        state["offset"] = max(state["offset"], u["update_id"])
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        # Only you can control the bot; ignore everyone else and /start.
        if str(msg.get("chat", {}).get("id")) != my_chat or not text or text.startswith("/start"):
            continue
        for reply in handle_command(text, state, now):
            send(reply, reply_to=msg.get("message_id"))
        handled += 1
    return handled


# --------------------------------------------------------------------- main


def main() -> None:
    global DRY_RUN
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replies-only", action="store_true", help="only answer replies, don't send the daily list")
    parser.add_argument("--once-per-day", action="store_true",
                        help="skip the daily list if one was already sent today (for scheduled + backup runs)")
    parser.add_argument("--preview", action="store_true", help="list upcoming events, send nothing")
    parser.add_argument("--dry-run", action="store_true", help="print today's list instead of sending it")
    parser.add_argument("--test", action="store_true", help="send a test Telegram message")
    parser.add_argument("--get-chat-id", action="store_true", help="find your Telegram chat ID")
    parser.add_argument("--now", help=argparse.SUPPRESS)  # for testing: pretend it's this time
    args = parser.parse_args()

    if args.get_chat_id:
        return cmd_get_chat_id()
    if args.test:
        send("✅ Your eDimension deadline bot is connected.")
        print("Test message sent.")
        return

    DRY_RUN = args.dry_run
    tz = ZoneInfo(os.environ.get("TIMEZONE") or "Asia/Singapore")
    remind_days = float(os.environ.get("REMIND_DAYS") or 3)
    exclude = os.environ.get("EXCLUDE_PATTERN", "").strip()
    now = datetime.fromisoformat(args.now).astimezone(tz) if args.now else datetime.now(tz)
    state = load_state()
    failed = False

    # 1. Answer "deadline N done" / "undo" first, so the numbers refer to the
    #    list you were looking at, not the one about to be sent.
    if not (args.preview or args.dry_run):
        try:
            n = process_replies(state, now)
            print(f"Answered {n} message(s).")
        except Exception as exc:
            print(f"Could not process replies: {exc}", file=sys.stderr)
            failed = True
        if args.replies_only:
            save_state(state, now)
            sys.exit(1 if failed else 0)
        if args.once_per_day and state.get("last_list_date") == now.date().isoformat():
            print("Today's list was already sent; only replies were checked.")
            save_state(state, now)
            sys.exit(1 if failed else 0)

    # 2. Today's list.
    try:
        cal = fetch_calendar(require_env("ICS_URL"))
    except Exception as exc:
        print(f"Could not read the eDimension calendar: {exc}", file=sys.stderr)
        if not (args.preview or args.dry_run) and os.environ.get("TELEGRAM_CHAT_ID"):
            try:
                send("⚠️ Deadline bot couldn't read your eDimension calendar. "
                     "The calendar link may have expired. Check the GitHub Actions log.")
            except Exception:
                pass
            save_state(state, now)
        sys.exit(1)

    deadlines = parse_deadlines(cal, tz)
    if exclude:
        deadlines = [d for d in deadlines if not re.search(exclude, d.title, re.I)]
    upcoming = [d for d in deadlines if d.due > now]

    if args.preview:
        print(f"{len(deadlines)} events in feed, {len(upcoming)} still upcoming. Next 20:")
        for d in upcoming[:20]:
            mark = "  [done]" if d.uid in state["done"] else ""
            print(f"  {d.due:%a %d %b %Y %H:%M}  {d.title}{mark}")
        return

    window_end = now + timedelta(days=remind_days)
    todays = [d for d in upcoming if d.due <= window_end and d.uid not in state["done"]]

    if not todays:
        print(f"Nothing due within {remind_days:g} days.")
    else:
        for text in build_messages([(d, False) for d in todays], now):
            send(text)
        print(f"Listed {len(todays)} deadline(s).")
        state["last_list"] = [d.to_dict() for d in todays]

    if not args.dry_run:
        state["last_list_date"] = now.date().isoformat()  # stops the backup run sending it twice
        save_state(state, now)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
