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
def say_hi():
    print("hi from a persisted job")

jobstores = {'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')}
scheduler = BackgroundScheduler(jobstores=jobstores)
scheduler.add_job(say_hi, 'interval', seconds=10, id='test_job', replace_existing=True)
scheduler.start()

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

### Runs a tool-calling loop: keeps calling the model and executing whatever
### tools it requests (read_tasks, add_task, complete_task, delete_task,
### modify_task) until it responds with plain text instead of a tool call,
### or until MAX_TOOL_ITERATIONS is hit.
def run_llm_pipeline(user_text: str, chat_id: int):
    now = datetime.now()
    date_context = f"Today is {now.strftime('%A, %Y-%m-%d')}, and the current time is {now.strftime('%H:%M:%S')}."
    print(chat_id)
    seed_prompt = (
        f"{date_context} "
        "You are a helpful assistant whose sole purpose is to help me remember what tasks "
        "I need to get done and motivate me to do it. Don't use any emoticons or emojis in your responses, "
        "keep them concise, and be serious. Act like the man in the chair in spy movies. "
        "We are texting, so keep your responses short and to the point. You have access to these tools: "
        "read_tasks: use when I ask what tasks I have pending. "
        "add_task: use when I ask you to add, create, or remember a new task. "
        "complete_task: use when I say I've finished or done a task. "
        "delete_task: use when I ask to remove, delete, or abandon a task. "
        "modify_task: use when I ask to change, update, or reschedule a task. "
        "read_jobs: use when I ask what reminders or check-ins I have scheduled "
        "(e.g. 'what reminders do I have', 'when's my next check-in'). "
        "Do not confuse this with read_tasks — tasks are things I need to do, "
        "jobs are reminders you've scheduled to message me about them. "
        "create_onetime_job: use when I ask to be reminded about something once, "
        "at a specific time or after a delay (e.g. 'remind me in 2 hours', "
        "'remind me tomorrow at 5pm'). "
        "create_recurring_job: use when I ask to be reminded of something every "
        "day at the same time (e.g. 'remind me of my tasks every day at 8am'). "
        "Never confuse the two — a reminder that repeats daily must use "
        "create_recurring_job, not create_onetime_job, even if I only mention one day first. "
        "complete_task, delete_task, and modify_task all require the task's numeric id. "
        "read_tasks is ONLY a lookup step — it is never a final answer on its own. "
        "Whenever my message implies an action (completing, deleting, or changing a task), "
        "you must end your turn by actually calling complete_task, delete_task, or modify_task "
        "with the matching id. Do not just summarize, list, or describe the task back to me — "
        "that is not a valid response to a request to complete, delete, or modify something. "
        "If you don't already know the id, call read_tasks first, then immediately follow up "
        "with the correct action tool using the id you found."
    )

    input_text = seed_prompt + " Now here is my message: " + user_text
    messages = [{"role": "user", "content": input_text}]

    available_tools = {
        "read_tasks": read_tasks,
        "add_task": add_task,
        "complete_task": complete_task,
        "delete_task": delete_task,
        "modify_task": modify_task,
        "read_jobs": read_jobs
    }
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
        run_llm_pipeline,
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
        run_llm_pipeline,
        trigger="date",
        run_date=run_date,
        args=[title, TELEGRAM_CHAT_ID],
    )
    return json.dumps({"status": "ok", "id": job.id})


bot.infinity_polling()