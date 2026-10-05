from datetime import date

import pytest


@pytest.fixture
def plan(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    import app.config
    importlib.reload(app.config)
    import app.plan
    return importlib.reload(app.plan)


def test_skip_and_undo(plan):
    d = date(2026, 10, 5)
    plan.set_skipped(d, ["m365-b|09:00|Daily", "m365-a|09:00|Sync"], True)
    assert plan.skipped_keys(d) == {"m365-b|09:00|Daily", "m365-a|09:00|Sync"}
    plan.set_skipped(d, ["m365-b|09:00|Daily"], False)
    assert plan.for_day(d)["skipped"] == ["m365-a|09:00|Sync"]


def test_activities_sorted_and_deleted(plan):
    d = date(2026, 10, 5)
    b = plan.add_activity(d, "14:00", "15:00", "Almoço")
    a = plan.add_activity(d, "08:00", "09:00", "Academia")
    assert [x["title"] for x in plan.for_day(d)["activities"]] == ["Academia", "Almoço"]
    assert plan.delete("activities", a["id"]) and not plan.delete("activities", a["id"])
    assert [x["id"] for x in plan.for_day(d)["activities"]] == [b["id"]]


def test_tasks_order_done_and_overdue(plan):
    today, yesterday = date(2026, 10, 5), date(2026, 10, 4)
    plan.add_task(today, "sem horário", None)
    t = plan.add_task(today, "às 9", "09:00")
    late = plan.add_task(yesterday, "ficou para trás", None)
    done = plan.add_task(yesterday, "feita", None)
    plan.update_task(done["id"], done=True)
    day = plan.for_day(today, today=today)
    assert [x["title"] for x in day["tasks"]] == ["às 9", "sem horário"]
    assert [x["title"] for x in day["overdue"]] == ["ficou para trás"]
    plan.update_task(late["id"], date=today.isoformat())
    assert plan.for_day(today, today=today)["overdue"] == []
    plan.update_task(t["id"], done=True)
    assert plan.for_day(today)["tasks"][-1]["title"] == "às 9"
    assert "overdue" not in plan.for_day(yesterday, today=today)


def test_todo_groups_and_nodate(plan):
    today = date(2026, 10, 5)
    plan.add_task(None, "sem data", None)
    plan.add_task(date(2026, 10, 1), "atrasada", None)
    plan.add_task(today, "hoje", "09:00")
    plan.add_task(date(2026, 10, 9), "depois", None)
    done = plan.add_task(today, "feita", None)
    plan.update_task(done["id"], done=True)
    t = plan.todo(today)
    assert [x["title"] for x in t["overdue"]] == ["atrasada"]
    assert [x["title"] for x in t["today"]] == ["hoje"]
    assert [x["title"] for x in t["later"]] == ["depois"]
    assert [x["title"] for x in t["nodate"]] == ["sem data"]
    assert [x["title"] for x in t["done"]] == ["feita"] and t["done"][0]["done_at"]
    # tarefa sem data não quebra as atrasadas do dia
    assert [x["title"] for x in plan.for_day(today, today=today)["overdue"]] == ["atrasada"]


def test_forget_account_removes_its_skips(plan):
    d = date(2026, 10, 5)
    plan.set_skipped(d, ["a|09:00|X", "b|10:00|Y"], True)
    plan.forget_account("a")
    assert plan.skipped_keys(d) == {"b|10:00|Y"}


def test_tags_are_cleaned_and_editable(plan):
    t = plan.add_task(None, "Proposta", None, ["#Cliente", "urgente", "cliente", " ", "x" * 40])
    assert t["tags"] == ["cliente", "urgente", "x" * 24]
    assert plan.update_task(t["id"], tags=["#SOC"])["tags"] == ["soc"]
    assert plan.update_task(t["id"], done=True)["tags"] == ["soc"]   # mudar outra coisa não apaga as tags
    assert plan.update_task(t["id"], tags=[])["tags"] == []
