### Query the DB and return a list of all pending task's fields
import json
import sqlite3

def read_tasks() -> str:
    """
    Retrieve and list all currently pending tasks.
    Use this ONLY when the user is asking to see, list, check, or review
    their existing tasks (e.g. "what do I need to do", "show my tasks"), or
    when you need to look up a task's id before completing, deleting, or
    modifying it.
    Do not use this to add or create anything.
    """
    conn = sqlite3.connect('taskmanager.db')
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    tasks = [dict(row) for row in cur.execute('SELECT * FROM tasks WHERE completed = 0')]

    print(json.dumps(tasks, indent=2))

    conn.close()

    return json.dumps(tasks)


### Insert a new task into the DB
def add_task(title: str, description: str, due_at: str = None) -> str:
    """
    Create and save a brand new task.
    Use this ONLY when the user wants to add, create, remember, or note down
    a new task (e.g. "remind me to...", "add a task to...", "I need to remember to...").
    Do not use this to view or list existing tasks.

    Args:
        title: short title of the task
        description: longer description of what the task involves
        due_at: optional due date/time for the task, formatted as "YYYY-MM-DD HH:MM:SS"
    """
    conn = sqlite3.connect('taskmanager.db')
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (title, description, dueAt) VALUES (?, ?, ?)",
            (title, description, due_at),
        )
        conn.commit()
        task_id = cur.lastrowid
    finally:
        conn.close()

    return json.dumps({"status": "ok", "id": task_id})


### Mark an existing task as completed
def complete_task(id: int) -> str:
    """
    Mark a task as completed.
    Use this when the user says they've finished, done, or completed a task.
    You must know the task's numeric id — if you don't, call read_tasks first
    to find the id of the task that matches what the user described.

    Args:
        id: the numeric id of the task to mark as completed
    """
    conn = sqlite3.connect('taskmanager.db')
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tasks SET completed = 1 WHERE id = ?", (id,))
        conn.commit()
        if cur.rowcount == 0:
            return json.dumps({"status": "error", "message": f"No task found with id {id}"})
    finally:
        conn.close()

    return json.dumps({"status": "ok", "id": id})


### Permanently delete a task
def delete_task(id: int) -> str:
    """
    Permanently delete a task.
    Use this when the user asks to remove or delete a task entirely.
    You must know the task's numeric id — if you don't, call read_tasks first
    to find the id of the task that matches what the user described.

    Args:
        id: the numeric id of the task to delete
    """
    conn = sqlite3.connect('taskmanager.db')
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM tasks WHERE id = ?", (id,))
        conn.commit()
        if cur.rowcount == 0:
            return json.dumps({"status": "error", "message": f"No task found with id {id}"})
    finally:
        conn.close()

    return json.dumps({"status": "ok", "id": id})


### Update one or more fields of an existing task
def modify_task(id: int, title: str = None, description: str = None, due_at: str = None) -> str:
    """
    Update one or more fields of an existing task (title, description, and/or due date).
    Use this when the user asks to change, update, reschedule, or edit a task.
    You must know the task's numeric id — if you don't, call read_tasks first
    to find the id of the task that matches what the user described.
    Only pass the fields that should actually change; leave the rest unset.

    Args:
        id: the numeric id of the task to modify
        title: new title for the task, if it should change
        description: new description for the task, if it should change
        due_at: new due date/time, formatted as "YYYY-MM-DD HH:MM:SS", if it should change
    """
    fields = []
    values = []
    if title is not None:
        fields.append("title = ?")
        values.append(title)
    if description is not None:
        fields.append("description = ?")
        values.append(description)
    if due_at is not None:
        fields.append("dueAt = ?")
        values.append(due_at)

    if not fields:
        return json.dumps({"status": "error", "message": "No fields provided to modify"})

    values.append(id)
    conn = sqlite3.connect('taskmanager.db')
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        if cur.rowcount == 0:
            return json.dumps({"status": "error", "message": f"No task found with id {id}"})
    finally:
        conn.close()

    return json.dumps({"status": "ok", "id": id})