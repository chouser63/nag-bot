import os
import telebot
import json
import anthropic
from datetime import datetime
from db import read_tasks, add_task, complete_task, delete_task, modify_task
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
import time

BOT_TOKEN = os.environ.get('BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

bot = telebot.TeleBot(BOT_TOKEN)

# Reads ANTHROPIC_API_KEY from the environment
claude = anthropic.Anthropic()
MODEL = os.environ.get('CLAUDE_MODEL', 'claude-haiku-5-5')

MAX_TOOL_ITERATIONS = 5  # safety cap so a confused model can't loop forever

### Scheduler Setup
jobstores = {'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')}
scheduler = BackgroundScheduler(jobstores=jobstores)
scheduler.start()
# the old 10-second test job is persisted in jobs.sqlite; clear it out
try:
    scheduler.remove_job('test_job')
except Exception:
    pass

### Handler for any Telegram message --> sends to LLM pipeline
@bot.message_handler(func=lambda msg: True)
def call_llm(message):
    run_llm_pipeline(message.text, message.chat.id)


def _tool(name: str, description: str, properties: dict = None, required: list = None) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
        },
    }

_ID = {"type": "integer", "description": "the numeric id of the task"}
_DUE = {"type": "string", "description": 'due date/time formatted as "YYYY-MM-DD HH:MM:SS"'}

# Tool schemas shown to Claude. The functions themselves live in db.py / below.
TOOL_SCHEMAS = {
    "read_tasks": _tool(
        "read_tasks",
        "List all pending tasks. Use when I ask what I need to do, or when you need a task's id "
        "before completing, deleting, or modifying it. Never use it to add anything."),
    "add_task": _tool(
        "add_task",
        "Create and save a brand new task. Use only when I want to add, create, or remember a task. "
        "This does NOT schedule a reminder.",
        {"title": {"type": "string", "description": "short title"},
         "description": {"type": "string", "description": "longer description of the task"},
         "due_at": _DUE},
        ["title", "description"]),
    "complete_task": _tool(
        "complete_task",
        "Mark a task as completed. Use when I say I finished a task. Requires the task id; "
        "call read_tasks first if you don't know it.",
        {"id": _ID}, ["id"]),
    "delete_task": _tool(
        "delete_task",
        "Permanently delete a task. Use when I ask to remove or abandon a task. Requires the task id; "
        "call read_tasks first if you don't know it.",
        {"id": _ID}, ["id"]),
    "modify_task": _tool(
        "modify_task",
        "Update the title, description, and/or due date of an existing task. Requires the task id. "
        "Only pass fields that should change.",
        {"id": _ID,
         "title": {"type": "string", "description": "new title"},
         "description": {"type": "string", "description": "new description"},
         "due_at": _DUE},
        ["id"]),
    "read_jobs": _tool(
        "read_jobs",
        "List all scheduled reminder jobs (check-ins). Use when I ask what reminders I have set. "
        "Not the same as read_tasks."),
    "create_onetime_job": _tool(
        "create_onetime_job",
        "Schedule a reminder that fires ONCE at a specific future date and time (e.g. 'remind me in 2 hours'). "
        "Calculate run_date yourself from the current date and time.",
        {"title": {"type": "string", "description": "short, self-contained description of what the reminder is about"},
         "run_date": {"type": "string", "description": 'when to send it, formatted "YYYY-MM-DD HH:MM:SS"'}},
        ["title", "run_date"]),
    "create_recurring_job": _tool(
        "create_recurring_job",
        "Schedule a reminder that repeats EVERY DAY at the same time (e.g. 'check in every day at 8am').",
        {"title": {"type": "string", "description": "short, self-contained description of what the reminder is about"},
         "hour": {"type": "integer", "description": "hour of day, 0-23"},
         "minute": {"type": "integer", "description": "minute, 0-59 (use 0 if not specified)"}},
        ["title", "hour"]),
}


def build_prompts(scheduled: bool, user_text: str) -> tuple[str, str]:
    """Returns (system_prompt, first_user_message)."""
    now = datetime.now()
    date_context = f"Today is {now.strftime('%A, %Y-%m-%d')}, and the current time is {now.strftime('%H:%M:%S')}."

    persona = (
        f"{date_context} "
        "You are an assistant whose sole purpose is to help me remember what tasks "
        "I need to get done and motivate me to do it. Don't use any emoticons or emojis in your responses, "
        "keep them concise, and be serious. Act like the man in the chair in spy movies. "
        "We are texting, so keep your responses short and to the point. "
    )

    if scheduled:
        system = (
            persona
            + "This is an AUTOMATIC scheduled check-in. I did not just send you a message; "
            "a timer you set earlier has fired, so you are the one initiating contact. "
            "Call read_tasks to see my pending tasks, then text me a short message reminding me "
            "of what is outstanding and asking for a status update. If there are no pending tasks, "
            "ask what I want to get done. Do not add, change, complete, or delete anything, "
            "and do not schedule anything. Always finish with a plain text message to me."
        )
        return system, f"[Scheduled check-in] Purpose: {user_text}"

    system = (
        persona
        + "TASKS are things I need to do. JOBS are scheduled check-ins: reminders you send me later. "
        "Never confuse the two. Adding a task does NOT schedule a reminder, and scheduling a reminder "
        "does NOT add a task. Only do what I actually asked for. "
        "A reminder that repeats daily must use create_recurring_job, even if I only mention one day first. "
        "read_tasks is only a lookup step, never a final answer on its own: if my message implies "
        "completing, deleting, or changing a task and you don't know its id, call read_tasks, then "
        "immediately call the action tool with the id you found."
    )
    return system, user_text


### Entry point for scheduled jobs: runs the pipeline in "system-initiated" mode
def run_scheduled_checkin(title: str, chat_id):
    run_llm_pipeline(title, chat_id, scheduled=True)


### Runs a tool-calling loop: keeps calling Claude and executing whatever tools
### it requests until it responds without a tool call, or until MAX_TOOL_ITERATIONS
### is hit. scheduled=True means a job fired (System -> User); False means the user texted us.
def run_llm_pipeline(user_text: str, chat_id, scheduled: bool = False):
    system, first_message = build_prompts(scheduled, user_text)
    messages = [{"role": "user", "content": first_message}]

    available_tools = {
        "read_tasks": read_tasks,
        "add_task": add_task,
        "complete_task": complete_task,
        "delete_task": delete_task,
        "modify_task": modify_task,
        "read_jobs": read_jobs,
        "create_recurring_job": create_recurring_job,
        "create_onetime_job": create_onetime_job,
    }
    if scheduled:
        available_tools = {"read_tasks": read_tasks}
    tools = [TOOL_SCHEMAS[name] for name in available_tools]

    reply = None
    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            response = claude.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=system,
                tools=tools,
                messages=messages,
            )

            if response.stop_reason == "refusal":
                break

            # Pass the full content back (includes any thinking blocks)
            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                reply = "".join(b.text for b in response.content if b.type == "text").strip()
                break

            results = []
            for call in tool_uses:
                func = available_tools.get(call.name)
                is_error = False
                if func is None:
                    result = json.dumps({"status": "error", "message": f"Unknown tool {call.name}"})
                    is_error = True
                else:
                    try:
                        result = func(**call.input)
                    except Exception as e:
                        result = json.dumps({"status": "error", "message": str(e)})
                        is_error = True
                print(f"[TOOL] {call.name}({call.input}) -> {result}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": str(result),
                    "is_error": is_error,
                })
            messages.append({"role": "user", "content": results})
    except anthropic.APIError as e:
        print(f"[LLM ERROR] {e}")

    bot.send_message(chat_id, reply or "Something went wrong processing that — try again?")


def read_jobs() -> str:
    """
        Retrieve and list all currently scheduled reminder jobs.
        Use this ONLY when I ask to see, list, check, or review my existing
        reminders or scheduled check-ins (e.g. "what reminders do I have set",
        "when is my next check-in"), or when you need to look up a job's id
        before canceling or modifying it.
        Do not use this to create a new reminder.
    """
    jobs = scheduler.get_jobs()
    job_list = [
        {
            "id": job.id,
            "name": job.name,
            "next_run_time": job.next_run_time.strftime("%Y-%m-%d %H:%M"),
            "trigger": str(job.trigger),
            "args": job.args,
        }
        for job in jobs
    ]
    return json.dumps(job_list)

def create_recurring_job(title: str, hour: int, minute: int = 0) -> str:
    """
    Schedule a reminder that repeats daily at the same time.
    Use this when I ask to be reminded of something every day at a specific
    time (e.g. "remind me of my tasks every day at 8am").
    Do not use this for a one-time reminder — use create_onetime_job for that.
    Do not use this to look up existing reminders — use read_jobs for that.

    Args:
        title: a short, self-contained description of what the reminder is
            about, written so it makes sense on its own later (e.g.
            "check in on pending tasks").
        hour: the hour (0-23) the reminder should repeat at every day.
        minute: the minute (0-59) the reminder should repeat at every day.
            Default to 0 if not specified.
    """
    job = scheduler.add_job(
        run_scheduled_checkin,
        trigger="cron",
        hour=hour,
        minute=minute,
        args=[title, TELEGRAM_CHAT_ID],
    )
    return json.dumps({"status": "ok", "id": job.id})

def create_onetime_job(title: str, run_date: str) -> str:
    """
    Schedule a reminder that fires once, at a specific future date and time.
    Use this when I ask to be reminded about something at a specific moment
    or after a delay (e.g. "remind me to call the dentist in 2 hours",
    "remind me to submit the report tomorrow at 5pm").
    Do not use this for something that repeats daily — use create_recurring_job for that.
    Do not use this to look up existing reminders — use read_jobs for that.

    Args:
        title: a short, self-contained description of what the reminder is
            about, written so it makes sense on its own later without any
            other context (e.g. "remind the user to call the dentist").
        run_date: the exact date and time to send the reminder, formatted as
            "YYYY-MM-DD HH:MM:SS". Calculate this yourself using the current
            date and time given above and what was asked (e.g. "in 2 hours"
            means 2 hours from the current time; "tomorrow at 5pm" means the
            next day's date at 17:00:00).
    """
    job = scheduler.add_job(
        run_scheduled_checkin,
        trigger="date",
        run_date=run_date,
        args=[title, TELEGRAM_CHAT_ID],
    )
    return json.dumps({"status": "ok", "id": job.id})


bot.infinity_polling()