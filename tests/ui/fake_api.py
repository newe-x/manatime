"""API de mentira para testar e fotografar o dashboard sem agendas reais.

Serve os arquivos de app/static direto do disco e responde /api/* com uma empresa fictícia.
Usado pelos testes de interface e por docs/make_screenshots.py.
"""

import json
import re
from datetime import date, timedelta
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "app" / "static"
TODAY = date(2026, 10, 5)            # segunda-feira; os testes congelam o relógio do navegador neste dia
NOW = "2026-10-05T10:15:00"

ACCOUNTS = [
    {"id": "acme", "name": "Acme Corp", "short": "ACM", "provider": "google", "color": "#a276e4", "url": None, "has_login": True},
    {"id": "globex", "name": "Globex", "short": "GLX", "provider": "outlook", "color": "#d92d20", "url": None, "has_login": True},
    {"id": "initech", "name": "Initech", "short": "INI", "provider": "outlook", "color": "#2468c9", "url": None, "has_login": True},
]


def ev(title, start, end, response="accepted", organizer=None, location="Teams", all_day=False):
    return {"title": title, "start": start, "end": end, "all_day": all_day, "location": location,
            "meeting_url": None, "organizer": organizer, "response": response}


# reuniões de exemplo por dia da semana (0 = segunda)
WEEK = {
    0: {"acme": [ev("Revisão do roadmap", "09:00", "09:45", organizer="Ana Lima"), ev("Revisão de contrato", "14:15", "15:00", organizer="Ana Lima")],
        "globex": [ev("Daily do time", "09:00", "09:15", organizer="Bruno Reis"), ev("Refinamento", "16:00", "16:45", "tentative")],
        "initech": [ev("Planejamento da sprint", "10:00", "11:00", "unknown", organizer="Teresa Nunes"),
                    ev("1:1 com gestão", "14:00", "14:30"), ev("Retrospectiva", "15:30", "16:30", "unknown")]},
    1: {"globex": [ev("Daily do time", "09:00", "09:15"), ev("Demo para cliente", "11:00", "12:00")],
        "initech": [ev("Arquitetura", "10:30", "11:30"), ev("Treinamento", "17:00", "18:00", "tentative")]},
    2: {"acme": [ev("Workshop de segurança", "13:00", "15:00", organizer="Ana Lima")],
        "globex": [ev("Daily do time", "09:00", "09:15")]},
    3: {"globex": [ev("Daily do time", "09:00", "09:15"), ev("Alinhamento comercial", "10:00", "10:45")],
        "initech": [ev("Comitê de mudanças", "10:00", "11:00"), ev("Code review", "15:00", "15:30")]},
    4: {"acme": [ev("Feriado municipal", None, None, all_day=True, location=None)],
        "globex": [ev("Daily do time", "09:00", "09:15"), ev("Happy hour", "17:30", "18:30", "tentative")],
        "initech": [ev("Retro mensal", "17:00", "18:00")]},
}


class FakeApi:
    def __init__(self):
        self.tasks = [
            {"id": "t1", "date": TODAY.isoformat(), "title": "Enviar proposta revisada", "time": "11:30", "done": False, "tags": ["acme", "urgente"]},
            {"id": "t2", "date": None, "title": "Ler RFC de observabilidade", "time": None, "done": False, "tags": ["estudo"]},
            {"id": "t3", "date": (TODAY - timedelta(days=1)).isoformat(), "title": "Responder e-mail do jurídico", "time": None, "done": False, "tags": []},
        ]
        self.skipped = {TODAY.isoformat(): ["globex|16:00|Refinamento"]}
        self.activities = {TODAY.isoformat(): [{"id": "a1", "date": TODAY.isoformat(), "start": "12:00", "end": "13:00", "title": "Almoço"}]}
        self.next_id = 100

    # ---------- montagem das respostas ----------
    def day(self, d: date) -> dict:
        evs = WEEK.get(d.weekday(), {})
        iso = d.isoformat()
        return {
            "date": iso,
            "plan": {"skipped": self.skipped.get(iso, []), "activities": self.activities.get(iso, []),
                     "tasks": [t for t in self.tasks if t["date"] == iso], "overdue": []},
            "accounts": [{**a, "status": "ok", "source": "json", "updated_at": f"{iso}T08:00:00", "events": evs.get(a["id"], [])}
                         for a in ACCOUNTS],
        }

    def week(self, d: date) -> dict:
        start = d - timedelta(days=(d.weekday() + 1) % 7)
        days = [self.day(start + timedelta(days=i)) for i in range(7)]
        per = {a["id"]: {"minutes": 0, "meetings": 0} for a in ACCOUNTS}
        for day in days:
            for a in day["accounts"]:
                for e in a["events"]:
                    if e["start"]:
                        h1, m1 = map(int, e["start"].split(":")); h2, m2 = map(int, e["end"].split(":"))
                        per[a["id"]]["minutes"] += h2 * 60 + m2 - h1 * 60 - m1
                        per[a["id"]]["meetings"] += 1
        busy = sum(v["minutes"] for v in per.values())
        totals = {"accounts": per, "busy_minutes": busy, "days_read": 7, "days_total": 7}
        month = {"accounts": {k: {"minutes": v["minutes"] * 4, "meetings": v["meetings"] * 4} for k, v in per.items()},
                 "busy_minutes": busy * 4, "days_read": 31, "days_total": 31}
        return {"start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat(), "month": d.strftime("%Y-%m"),
                "days": days, "totals": {"week": totals, "month": month}}

    def todo(self) -> dict:
        iso = TODAY.isoformat()
        op = [t for t in self.tasks if not t["done"]]
        return {"overdue": [t for t in op if t["date"] and t["date"] < iso], "today": [t for t in op if t["date"] == iso],
                "later": [t for t in op if t["date"] and t["date"] > iso], "nodate": [t for t in op if not t["date"]],
                "done": [t for t in self.tasks if t["done"]]}

    # ---------- roteamento ----------
    def handle(self, method: str, path: str, query: dict, body: dict | None):
        d = date.fromisoformat(query.get("date") or TODAY.isoformat())
        if path == "/api/config":
            return {"novnc_port": 6080}
        if path == "/api/agenda":
            return self.day(d)
        if path == "/api/week":
            return self.week(d)
        if path == "/api/refresh":
            return {"day": self.day(d), "week": self.week(d)}
        if path == "/api/todo":
            return self.todo()
        if path == "/api/accounts" and method == "GET":
            return {"accounts": ACCOUNTS, "colors": ["#a276e4", "#d92d20", "#2468c9", "#b88a0a", "#d6409f", "#5f8a1f"],
                    "providers": ["google", "outlook", "outlook_live", "custom"]}
        if path == "/api/plan/tasks" and method == "POST":
            self.next_id += 1
            t = {"id": f"t{self.next_id}", "date": body.get("date"), "title": body["title"], "time": body.get("time"),
                 "done": False, "tags": body.get("tags", [])}
            self.tasks.append(t)
            return t
        m = re.fullmatch(r"/api/plan/tasks/(\w+)", path)
        if m:
            t = next(x for x in self.tasks if x["id"] == m.group(1))
            if method == "DELETE":
                self.tasks.remove(t)
                return {"ok": True}
            t.update({k: v for k, v in (body or {}).items() if v is not None})
            return t
        if path == "/api/plan/skip":
            keys = set(self.skipped.get(body["date"], []))
            keys = keys | set(body["keys"]) if body["skip"] else keys - set(body["keys"])
            self.skipped[body["date"]] = sorted(keys)
            return self.day(date.fromisoformat(body["date"]))["plan"]
        if path == "/api/plan/activities" and method == "POST":
            self.next_id += 1
            a = {"id": f"a{self.next_id}", **body}
            self.activities.setdefault(body["date"], []).append(a)
            return a
        return {"ok": True}


FREEZE_CLOCK = """(() => { const T = new Date('%s').getTime(), D0 = Date.now(); const R = Date;
  class F extends R { constructor(...a) { super(...(a.length ? a : [T + (R.now() - D0)])); } static now() { return T + (R.now() - D0); } }
  window.Date = F; })();""" % NOW


def install(page, api: FakeApi | None = None) -> FakeApi:
    """Liga a página à API falsa: arquivos do disco + /api simulada + relógio congelado."""
    api = api or FakeApi()
    page.add_init_script(FREEZE_CLOCK)

    def route(r):
        from urllib.parse import parse_qs, urlparse
        u = urlparse(r.request.url)
        if u.path.startswith("/api/"):
            body = json.loads(r.request.post_data) if r.request.post_data else None
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            r.fulfill(json=api.handle(r.request.method, u.path, q, body))
        elif u.path == "/":
            r.fulfill(path=STATIC / "index.html")
        elif u.path.startswith("/static/"):
            r.fulfill(path=STATIC / u.path.removeprefix("/static/"))
        else:
            r.abort()

    page.route("http://manatime.test/**", route)
    return api


URL = "http://manatime.test/"
