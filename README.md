# eDimension Deadline Bot

A Telegram bot that sends you a morning list of your SUTD eDimension deadlines due in the next 3 days. You can tick deadlines off by replying to it.

> ⏰ **Upcoming eDimension deadlines**
>
> ~~1. **Problem Set 3**
>      Due Wed 18 Nov, 11:59 PM (in 1d 14h)~~
>
> 2. **Design Report**
>      Due Thu 19 Nov, 11:59 PM (in 2d 14h)

It runs free on **your own** GitHub account. You don't need to install anything. Setup takes about 10 minutes.

## Setup

### 1. Make your own copy
Click **Use this template → Create a new repository** at the top of this page.
Set it to **Private**. Your copy will store your deadline titles, so don't make it public.

### 2. Get your eDimension calendar link
eDimension → **Calendar** (left menu) → settings (gear icon) → **Get external calendar link** → copy it.
Treat it like a password: anyone with this link can see your deadlines.

### 3. Create your Telegram bot
1. In Telegram, message **@BotFather**, send `/newbot`, and pick a name and a username ending in `bot`.
2. Copy the **token** it gives you, which looks like `123456789:AAH...`.
3. Open your new bot, press **Start**, and send it `hi`.

### 4. Find your chat ID
In a browser, open this address, replacing `<TOKEN>` with your token:
```
https://api.telegram.org/bot<TOKEN>/getUpdates
```
Look for `"chat":{"id":` followed by a number. That number is your chat ID.
If the page only shows `"result":[]`, send your bot another message and refresh.

### 5. Add your settings as secrets
In **your copy** on GitHub, go to **Settings → Secrets and variables → Actions → New repository secret**. Add these three, one at a time:

| Name | Secret |
|---|---|
| `ICS_URL` | your eDimension calendar link |
| `TELEGRAM_BOT_TOKEN` | your bot token |
| `TELEGRAM_CHAT_ID` | your chat ID |

Type the name exactly as shown, and paste only the value into the Secret box.

### 6. Turn it on
Go to **Actions** and click **"I understand my workflows, go ahead and enable them"** if GitHub shows it. Then go to **Deadline reminders → Run workflow → Run workflow**.
Within about a minute you should get your first list in Telegram. If the run shows a red ✗, the most likely cause is a typo in a secret name.

## Using it

| You send the bot | What happens |
|---|---|
| `deadline 1 done` | Deadline 1 is struck out and won't appear in future lists |
| `deadline 1 3 done` | Ticks off several at once |
| `undo` | Brings back the last one(s) you ticked off |

The bot isn't online all the time. It wakes up at about **7:47, 8:47 and 9:47 AM** (Singapore time) and answers your replies then.
- **7:47:** sends the day's list.
- **9:47:** sends the list as a backup if GitHub skipped the 7:47 run.
- **Late replies:** anything you send after 9:47 is answered the next morning.
- **Delays:** GitHub can start runs a little late.

Each person's bot uses about **90 of their own 2,000 free GitHub minutes a month**.

## Good to know
- **Only due dates set in eDimension count.** Deadlines that only appear in announcements or PDFs won't be picked up.
- **Lectures are skipped.** Calendar items with a room set are ignored.
- **Your bot only listens to you.** It ignores messages from any other chat.
- **If your calendar link stops working,** the bot messages you ⚠️. Get a fresh link and update the `ICS_URL` secret.
- **Changing the times:** edit the `cron` lines in `.github/workflows/reminder.yml`. They are in UTC, and Singapore is UTC+8.

## Running it on your own computer (optional)
You need Python 3.11 or newer. Copy `.env.example` to `.env`, fill it in, then:
```
pip install -r requirements.txt
python reminder.py --preview    # list what's in your calendar
python reminder.py --dry-run    # print today's list without sending it
python reminder.py --test       # send a test message to Telegram
```
