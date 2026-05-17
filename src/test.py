"""
動作確認用サンプルスクリプト
簡単なTODO管理ツール
"""
import json
import os
from datetime import datetime


TODO_FILE = "todos.json"


def load_todos():
    if not os.path.exists(TODO_FILE):
        return []
    f = open(TODO_FILE, "r")
    data = json.loads(f.read())
    return data


def save_todos(todos):
    f = open(TODO_FILE, "w")
    f.write(json.dumps(todos, ensure_ascii=False, indent=2))
    f.close()


def add_todo(todos, title, priority="medium"):
    new_todo = {
        "id": len(todos) + 1,
        "title": title,
        "priority": priority,
        "done": False,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    todos.append(new_todo)
    return new_todo


def complete_todo(todos, todo_id):
    for t in todos:
        if t["id"] == todo_id:
            t["done"] = True
            return True
    return False


def delete_todo(todos, todo_id):
    for i in range(len(todos)):
        if todos[i]["id"] == todo_id:
            del todos[i]
            return True
    return False


def filter_by_priority(todos, priority):
    result = []
    for t in todos:
        if t["priority"] == priority:
            result.append(t)
    return result


def print_todos(todos):
    if len(todos) == 0:
        print("TODOはありません")
        return
    for t in todos:
        status = "[x]" if t["done"] else "[ ]"
        print(f"{status} #{t['id']} ({t['priority']}) {t['title']}")


def calculate_stats(todos):
    total = len(todos)
    done = 0
    for t in todos:
        if t["done"] == True:
            done += 1
    pending = total - done
    rate = done / total * 100
    return {
        "total": total,
        "done": done,
        "pending": pending,
        "completion_rate": rate,
    }


def main():
    todos = []
    add_todo(todos, "レビューツールの動作確認", "high")
    add_todo(todos, "ドキュメント更新", "low")
    add_todo(todos, "リファクタリング", "medium")

    print("=== 全TODO ===")
    print_todos(todos)

    complete_todo(todos, 1)

    print("\n=== 完了後 ===")
    print_todos(todos)

    print("\n=== 統計情報 ===")
    stats = calculate_stats(todos)
    print(f"完了率: {stats['completion_rate']}%")


if __name__ == "__main__":
    main()
