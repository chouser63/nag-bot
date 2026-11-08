# NagBot

A personal assistant that keeps you focused on the tasks you want to get done each day and week. It checks in with you over Telegram, and you reply with your progress. When you finish something, tell it and it stops bothering you about it.

## How it works

Two flows, both handled by one orchestrator process (`bot.py`):

- **System → User:** a scheduled job fires, the orchestrator reads your pending tasks from the DB, has Claude write a check-in message, and sends it to you on Telegram.
- **User → System:** you text the bot, Claude decides which tool to call (add, complete, modify, or delete a task, or schedule a reminder), the DB is updated, and Claude sends a confirmation.

## Architecture

| Piece | Choice |
|---|---|
| Reasoning | Claude API (`claude-haiku-5-5` by default, override with `CLAUDE_MODEL`) |
| Task storage | SQLite (`taskmanager.db`) |
| Messaging | Telegram (`pyTelegramBotAPI`) |
| Scheduler | APScheduler with a persistent job store (`jobs.sqlite`) |

Tasks (things you need to do) and jobs (scheduled check-ins) are separate concepts. Adding a task does not schedule a reminder, and scheduling a reminder does not add a task.

## Files

- `bot.py`: Telegram handler, Claude tool loop, prompts, scheduler, and job tools
- `db.py`: task tools (`read_tasks`, `add_task`, `complete_task`, `delete_task`, `modify_task`)

## Setup

1. Create a bot with @BotFather on Telegram to get a token. Get your chat id from @userinfobot.
2. Get an Anthropic API key from console.anthropic.com.
3. Install and create the database:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
sqlite3 taskmanager.db "CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT, dueAt TEXT, completed INTEGER DEFAULT 0);"
```

4. Set the environment variables and run. Never commit these.

```bash
export BOT_TOKEN='...'
export TELEGRAM_CHAT_ID='...'
export ANTHROPIC_API_KEY='...'
python3 bot.py
```

## Usage examples

- "add a task: finish lab report, due Friday 5pm"
- "what do I need to do?"
- "finished the lab report"
- "check in with me every day at 8am"
- "remind me in 2 hours to stretch"
- "what reminders do I have?"

## Status

Working: task CRUD through chat, one-time and daily recurring reminders, persistent jobs, and a separate prompt for system-initiated check-ins.

## Roadmap

- **Morning check-in:** every morning the bot asks what you want to get done that day, then checks back in on those items.
- **Motivation rating:** each task gets a rating. At a low rating the bot checks in rarely and is gentle. At a high rating it checks in often and acts like a drill sergeant.
- **Deadline-aware nagging:** the bot works out how close each task is to its deadline and adjusts both its tone and its schedule. Motivation rating feeds into this. The bot also chooses its own next wake-up time by scheduling a job for itself after each check-in:
  - Far from the deadline: it checks in every few days.
  - Close to the deadline: it schedules check-ins more and more frequently.
  - Done: it stops.
- **Long-term goals:** goals such as studying for a test or working on a project, which the bot checks in on periodically.
- **Cleanup:** a committed schema or init script, job cancel and modify tools, and an allowlist so only your chat id can use the bot.
