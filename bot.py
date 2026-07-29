import os
import telebot
import requests
import json
from ollama import chat
from datetime import datetime
from db import read_tasks, add_task, complete_task, delete_task, modify_task

BOT_TOKEN = os.environ.get('BOT_TOKEN')
LLM_API_URL = os.environ.get('LLM_API_URL')

bot = telebot.TeleBot(BOT_TOKEN)

MAX_TOOL_ITERATIONS = 5  # safety cap so a confused model can't loop forever

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

### Handler function for any Telegram message.
### Runs a tool-calling loop: keeps calling the model and executing whatever
### tools it requests (read_tasks, add_task, complete_task, delete_task,
### modify_task) until it responds with plain text instead of a tool call,
### or until MAX_TOOL_ITERATIONS is hit.
@bot.message_handler(func=lambda msg: True)
def call_llm(message):
    now = datetime.now()
    date_context = f"Today is {now.strftime('%A, %Y-%m-%d')}, and the current time is {now.strftime('%H:%M:%S')}."

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
        "complete_task, delete_task, and modify_task all require the task's numeric id. "
        "read_tasks is ONLY a lookup step — it is never a final answer on its own. "
        "Whenever my message implies an action (completing, deleting, or changing a task), "
        "you must end your turn by actually calling complete_task, delete_task, or modify_task "
        "with the matching id. Do not just summarize, list, or describe the task back to me — "
        "that is not a valid response to a request to complete, delete, or modify something. "
        "If you don't already know the id, call read_tasks first, then immediately follow up "
        "with the correct action tool using the id you found."
    )

    input_text = seed_prompt + " Now here is my message: " + message.text
    messages = [{"role": "user", "content": input_text}]

    available_tools = {
        "read_tasks": read_tasks,
        "add_task": add_task,
        "complete_task": complete_task,
        "delete_task": delete_task,
        "modify_task": modify_task,
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
        bot.send_message(message.chat.id, response.message.content)
    else:
        bot.send_message(message.chat.id, "Something went wrong processing that — try again?")

bot.infinity_polling()