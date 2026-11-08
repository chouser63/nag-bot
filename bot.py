import os
import telebot
import requests
import json
from ollama import chat
from datetime import datetime
from db import read_tasks, add_task, complete_task, delete_task, modify_task
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
import time

BOT_TOKEN = os.environ.get('BOT_TOKEN')
LLM_API_URL = os.environ.get('LLM_API_URL')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

bot = telebot.TeleBot(BOT_TOKEN)

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

### Basic utility function to call the Ollama API
def get_llm_response(message: str) -> str:

    body = {
        "model": "qwen3:4b-instruct-2507-q4_K_M",
        "messages": [
                    {"role": "user", "content": message}
        ],
        "stream": False,
        "options": {
                    "num_predict": 150
        }
    }
    response = requests.post(LLM_API_URL, json=body, timeout=180)
    data = response.json()
    return data["message"]["content"]

### Handler for any Telegram message --> sends to LLM pipeline
@bot.message_handler(func=lambda msg: True)
def call_llm(message):
    run_llm_pipeline(message.text, message.chat.id)

def build_seed_prompt(scheduled: bool, user_text: str) -> str:
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
        return (
            persona
            + "This is an AUTOMATIC scheduled check-in. I did not just send you a message; "
            "a timer you set earlier has fired, so you are the one initiating contact. "
            f"The purpose of this check-in is: {user_text}. "
            "Call read_tasks to see my pending tasks, then text me a short message reminding me "
            "of what is outstanding and asking for a status update. If there are no pending tasks, "
            "ask what I want to get done. Do not add, change, complete, or delete anything, "
            "and do not schedule anything. Always finish with a plain text message to me."
        )

    return (
        persona
        + "You have access to these tools. "
        "TASKS are things I need to do. JOBS are scheduled check-ins: reminders you send me later. "
        "Never confuse the two. Adding a task does NOT schedule a reminder, and scheduling a reminder "
        "does NOT add a task. Only do what I actually asked for. "
        "Task tools: "
        "read_tasks: use when I ask what tasks I have pending. "
        "add_task: use when I ask you to add, create, or remember a new task. "
        "complete_task: use when I say I've finished or done a task. "
        "delete_task: use when I ask to remove, delete, or abandon a task. "
        "modify_task: use when I ask to change, update, or edit a task. "
        "Job tools: "
        "read_jobs: use when I ask what reminders or check-ins I have scheduled. "
        "create_onetime_job: use when I ask to be reminded once, at a specific time or after a delay "
        "(e.g. 'remind me in 2 hours', 'remind me tomorrow at 5pm'). "
        "create_recurring_job: use when I ask to be reminded every day at the same time "
        "(e.g. 'check in with me every day at 8am'). A reminder that repeats daily must use "
        "create_recurring_job, even if I only mention one day first. "
        "complete_task, delete_task, and modify_task all require the task's numeric id. "
        "read_tasks is ONLY a lookup step, never a final answer on its own. "
        "Whenever my message implies completing, deleting, or changing a task, you must end by "
        "actually calling complete_task, delete_task, or modify_task with the matching id. "
        "If you don't know the id, call read_tasks first, then immediately call the action tool. "
        "Now here is my message: " + user_text
    )


### Entry point for scheduled jobs: runs the pipeline in "system-initiated" mode
def run_scheduled_checkin(title: str, chat_id):
    run_llm_pipeline(title, chat_id, scheduled=True)


### Runs a tool-calling loop: keeps calling the model and executing whatever
### tools it requests until it responds with plain text instead of a tool call,
### or until MAX_TOOL_ITERATIONS is hit.
### scheduled=True means a job fired (System -> User); False means the user texted us.
def run_llm_pipeline(user_text: str, chat_id, scheduled: bool = False):
    messages = [{"role": "user", "content": build_seed_prompt(scheduled, user_text)}]

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
    tools = list(available_tools.values())

    response = None
    for _ in range(MAX_TOOL_ITERATIONS):
        response = chat(model="qwen3:4b-instruct-2507-q4_K_M", tools=tools, messages=messages, think=False)
        print(messages)
        messages.append(response.message)

        if not response.message.tool_calls:
            break

        print("[TOOL CALLS]:", response.message.tool_calls)

        for call in response.message.tool_calls:
            func = available_tools.get(call.function.name)
            if func is None:
                result = json.dumps({"status": "error", "message": f"Unknown tool {call.function.name}"})
            else:
                try:
                    result = func(**call.function.arguments)
                except Exception as e:
                    result = json.dumps({"status": "error", "message": str(e)})
            print(f"[TOOL] {call.function.name}({call.function.arguments}) -> {result}")
            messages.append({"role": "tool", "tool_name": call.function.name, "content": str(result)})

    if response and response.message.content:
        bot.send_message(chat_id, response.message.content)
    else:
        bot.send_message(chat_id, "Something went wrong processing that — try again?")

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

def create_recurring_job(title: str, hour: int, minute: int) -> str:
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