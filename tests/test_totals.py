import json
from datetime import date

import pytest


@pytest.fixture
def main(tmp_path, monkeypatch):
    cfg = tmp_path / "accounts.yaml"
    cfg.write_text("accounts:\n  - {id: a, name: A, provider: outlook}\n  - {id: b, name: B, provider: outlook}\n")
    monkeypatch.setenv("ACCOUNTS_FILE", str(cfg))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    import app.config
    importlib.reload(app.config)   # relê as variáveis de ambiente antes de app.main importar a config
    import app.plan
    importlib.reload(app.plan)
    import app.main
    return importlib.reload(app.main)


def ev(start, end, response="accepted", all_day=False):
    return {"title": "x", "start": start, "end": end, "all_day": all_day, "location": None,
            "meeting_url": None, "organizer": None, "response": response}


def test_busy_minutes_merges_overlaps_and_skips_declined(main):
    assert main.busy_minutes([ev("09:00", "10:00"), ev("09:30", "10:30"), ev("11:00", "11:30", "declined"),
                              ev(None, None, all_day=True)]) == 90


def test_total_counts_cross_company_overlap_once(main):
    day = date(2026, 10, 5)
    main.write_cache(day, {"a": {"status": "ok", "events": [ev("09:00", "10:00")]},
                           "b": {"status": "ok", "events": [ev("09:30", "10:30")]}})
    t = main.totals([day, date(2026, 10, 6)])
    assert t["accounts"]["a"]["minutes"] == 60 and t["accounts"]["b"]["minutes"] == 60
    assert t["busy_minutes"] == 90
    assert (t["days_read"], t["days_total"]) == (1, 2)


def test_prune_cache_keeps_recent_days(main, monkeypatch):
    monkeypatch.setattr(main, "CACHE_DAYS", 10)
    old, recent = date(2000, 1, 1), date.today()
    main.write_cache(old, {}); main.write_cache(recent, {})
    main.prune_cache()
    assert not main.cache_file(old).exists() and main.cache_file(recent).exists()


def test_skipped_meeting_leaves_totals(main):
    day = date(2026, 10, 5)
    main.write_cache(day, {"a": {"status": "ok", "events": [ev("09:00", "10:00")]},
                           "b": {"status": "ok", "events": [ev("09:30", "10:30")]}})
    import app.plan
    app.plan.set_skipped(day, ["b|09:30|x"], True)
    t = main.totals([day])
    assert t["accounts"]["b"]["minutes"] == 0 and t["busy_minutes"] == 60
