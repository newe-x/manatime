"""O que você planeja por cima das agendas: reuniões que não vai, atividades e tarefas.

Fica só aqui (data/plan.json); nada disso é enviado para o Google ou o Outlook.
"""

import json
import os
import threading
import uuid
from datetime import date, datetime, timedelta

from .config import DATA_DIR

PLAN_FILE = DATA_DIR / "plan.json"
OVERDUE_DAYS = 30   # tarefas não feitas de até 30 dias atrás aparecem em "de dias anteriores"
_lock = threading.Lock()


def event_key(account_id: str, event: dict) -> str:
    """Mesma chave que o dashboard usa para identificar uma reunião no dia."""
    return f"{account_id}|{event['start'] or 'dia'}|{event['title']}"


def _empty() -> dict:
    return {"skipped": {}, "activities": [], "tasks": []}


def _read() -> dict:
    if not PLAN_FILE.exists():
        return _empty()
    data = json.loads(PLAN_FILE.read_text())
    return {**_empty(), **data}


def _write(data: dict) -> None:
    PLAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLAN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, PLAN_FILE)   # troca atômica: um travamento no meio não corrompe o plano


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _by_time(item: dict) -> tuple:
    return (item.get("time") or item.get("start") or "99:99", item.get("created", ""))


def for_day(day: date, today: date | None = None) -> dict:
    data = _read()
    iso = day.isoformat()
    out = {
        "skipped": data["skipped"].get(iso, []),
        "activities": sorted((a for a in data["activities"] if a["date"] == iso), key=_by_time),
        "tasks": sorted((t for t in data["tasks"] if t["date"] == iso), key=lambda t: (t["done"], *_by_time(t))),
    }
    if today is not None and day == today:
        oldest = (today - timedelta(days=OVERDUE_DAYS)).isoformat()
        out["overdue"] = sorted((t for t in data["tasks"] if t["date"] and oldest <= t["date"] < iso and not t["done"]),
                                key=lambda t: (t["date"], *_by_time(t)))
    return out


def skipped_keys(day: date) -> set[str]:
    return set(_read()["skipped"].get(day.isoformat(), []))


def set_skipped(day: date, keys: list[str], skip: bool) -> None:
    with _lock:
        data = _read()
        current = set(data["skipped"].get(day.isoformat(), []))
        current = current | set(keys) if skip else current - set(keys)
        if current:
            data["skipped"][day.isoformat()] = sorted(current)
        else:
            data["skipped"].pop(day.isoformat(), None)
        _write(data)


def add_activity(day: date, start: str, end: str, title: str) -> dict:
    item = {"id": _new_id(), "date": day.isoformat(), "start": start, "end": end, "title": title,
            "created": datetime.now().isoformat(timespec="seconds")}
    with _lock:
        data = _read()
        data["activities"].append(item)
        _write(data)
    return item


MAX_TAGS = 6


def clean_tags(tags) -> list[str]:
    """Minúsculas, sem "#", sem repetidas, no máximo 6 de até 24 caracteres."""
    out = []
    for t in tags or []:
        t = str(t).strip().lstrip("#").strip().lower()[:24]
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def add_task(day: date | None, title: str, time: str | None, tags=None) -> dict:
    item = {"id": _new_id(), "date": day.isoformat() if day else None, "title": title, "time": time, "done": False,
            "tags": clean_tags(tags),
            "created": datetime.now().isoformat(timespec="seconds")}
    with _lock:
        data = _read()
        data["tasks"].append(item)
        _write(data)
    return item


def update_task(task_id: str, **changes) -> dict | None:
    with _lock:
        data = _read()
        for task in data["tasks"]:
            if task["id"] == task_id:
                if changes.get("tags") is not None:
                    changes["tags"] = clean_tags(changes["tags"])
                task.update({k: v for k, v in changes.items() if v is not None})
                if changes.get("done") is True:
                    task["done_at"] = datetime.now().isoformat(timespec="seconds")
                elif changes.get("done") is False:
                    task.pop("done_at", None)
                _write(data)
                return task
    return None


def todo(today: date) -> dict:
    """To-do geral: abertas (atrasadas, de hoje, futuras, sem data) e as últimas feitas."""
    tasks = _read()["tasks"]
    iso = today.isoformat()
    open_ = [t for t in tasks if not t["done"]]
    key = lambda t: (t["date"] or "9999", t.get("time") or "99:99", t.get("created", ""))
    done = sorted((t for t in tasks if t["done"]), key=lambda t: t.get("done_at", ""), reverse=True)[:20]
    return {
        "overdue": sorted((t for t in open_ if t["date"] and t["date"] < iso), key=key),
        "today": sorted((t for t in open_ if t["date"] == iso), key=key),
        "later": sorted((t for t in open_ if t["date"] and t["date"] > iso), key=key),
        "nodate": sorted((t for t in open_ if not t["date"]), key=lambda t: t.get("created", "")),
        "done": done,
    }


def forget_account(account_id: str) -> None:
    """Tira as escolhas de "não vou" de uma conta removida."""
    with _lock:
        data = _read()
        for day, keys in list(data["skipped"].items()):
            kept = [k for k in keys if not k.startswith(f"{account_id}|")]
            if kept:
                data["skipped"][day] = kept
            else:
                data["skipped"].pop(day)
        _write(data)


def delete(kind: str, item_id: str) -> bool:
    with _lock:
        data = _read()
        before = len(data[kind])
        data[kind] = [x for x in data[kind] if x["id"] != item_id]
        if len(data[kind]) == before:
            return False
        _write(data)
        return True
