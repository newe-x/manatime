import asyncio
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from datetime import date as Date   # nos modelos abaixo o campo se chama "date" e esconderia o tipo
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .browser import BrowserPool
from .config import (
    CACHE_DAYS, CACHE_DIR, DEBUG_DIR, NOVNC_PORT, REFRESH_HOURS, REFRESH_MINUTES, REFRESH_WEEKDAYS_ONLY,
    ACCOUNT_COLORS, DAY_URLS, PROFILES_DIR, PROVIDERS, Account, days_between, load_accounts, month_range,
    save_accounts, slug, week_start,
)
from . import plan
from .parser import extract_range

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agenda")

STATIC = Path(__file__).parent / "static"
MONTH_REFRESH_SECONDS = 60 * 60   # o mês inteiro é relido no máximo uma vez por hora

accounts = load_accounts()
pool = BrowserPool(accounts)
refresh_lock = asyncio.Lock()


def cache_file(day: date) -> Path:
    return CACHE_DIR / f"{day.isoformat()}.json"


def read_cache(day: date) -> dict:
    path = cache_file(day)
    return json.loads(path.read_text()) if path.exists() else {}


def write_cache(day: date, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file(day).write_text(json.dumps(data, ensure_ascii=False, indent=2))


def prune_cache() -> None:
    """Apaga dias com mais de CACHE_DAYS; o cache cresceria um arquivo por dia para sempre."""
    oldest = date.today() - timedelta(days=CACHE_DAYS)
    removed = 0
    for path in CACHE_DIR.glob("*.json"):
        try:
            if date.fromisoformat(path.stem) < oldest:
                path.unlink()
                removed += 1
        except ValueError:
            continue
    if removed:
        log.info("cache: %d dias antigos apagados", removed)


def write_debug(account_id: str, start: date, end: date, snapshot: dict, source: str) -> None:
    """Guarda o que foi lido da página, para ajustar o parser se o Google/Outlook mudarem o layout."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    dump = {"start": start.isoformat(), "end": end.isoformat(), "url": snapshot.get("url"), "source": source,
            "json_seen": snapshot.get("json_seen"), "pages": snapshot.get("pages"),
            "json_events": snapshot.get("json_events")}
    (DEBUG_DIR / f"{account_id}.json").write_text(json.dumps(dump, ensure_ascii=False, indent=2, default=str))


async def read_account(account_id: str, start: date, end: date) -> dict[date, dict]:
    """Lê [start, end) de uma conta e devolve o registro de cada dia."""
    browser = pool.browsers[account_id]
    now = datetime.now().isoformat(timespec="seconds")
    days = days_between(start, end)

    def every_day(record: dict) -> dict[date, dict]:
        return {d: dict(record) for d in days}

    try:
        snapshot = await browser.snapshot(start, end)
        # O Outlook às vezes abre sem pedir os eventos (redirecionamento, cache); sem eles daria
        # um "dia livre" falso. Tenta mais uma vez e, se continuar, é erro.
        if browser.account.provider in ("outlook", "outlook_live") and not snapshot["login_required"] and not snapshot["json_seen"]:
            log.warning("%s: Outlook não enviou os eventos; tentando de novo", account_id)
            snapshot = await browser.snapshot(start, end)
            if not snapshot["login_required"] and not snapshot["json_seen"]:
                write_debug(account_id, start, end, snapshot, "none")
                return every_day({"status": "error", "events": [], "updated_at": now, "error": "calendar_no_data"})
        if snapshot["login_required"]:
            return every_day({"status": "login_required", "events": [], "updated_at": now})
        by_day, missing, source = extract_range(start, end, snapshot)
        write_debug(account_id, start, end, snapshot, source)
        out = {}
        for d in days:
            out[d] = {"status": "ok", "events": by_day[d], "source": source, "updated_at": now}
            if d in missing:
                out[d]["missing"] = missing[d]
        if missing:
            log.warning("%s: o calendário anuncia eventos que não foram lidos (%s)", account_id, sorted(missing)[0])
        return out
    except Exception as e:
        log.exception("falha ao ler %s", account_id)
        return every_day({"status": "error", "events": [], "updated_at": now, "error": f"{type(e).__name__}: {e}"})


async def refresh_range(start: date, end: date) -> None:
    async with refresh_lock:
        t0 = time.monotonic()
        results = await asyncio.gather(*(read_account(a.id, start, end) for a in accounts))
        for d in days_between(start, end):
            previous = read_cache(d)
            data = {}
            for account, per_day in zip(accounts, results):
                record = per_day[d]
                # Mantém as últimas reuniões boas se esta leitura falhou
                old = previous.get(account.id, {})
                if record["status"] != "ok" and (old.get("status") == "ok" or old.get("stale")):
                    record["events"] = old["events"]
                    record["stale"] = True
                    record["read_at"] = old.get("read_at") or old.get("updated_at")
                data[account.id] = record
            write_cache(d, data)
        log.info("lidos %s a %s em %.0fs", start, end - timedelta(days=1), time.monotonic() - t0)


def week_of(day: date) -> tuple[date, date]:
    first = week_start(day)
    return first, first + timedelta(days=7)


async def ensure(start: date, end: date) -> None:
    if any(not cache_file(d).exists() for d in days_between(start, end)):
        await refresh_range(start, end)


# ---------- Horas por empresa ----------

def busy_minutes(events: list[dict]) -> int:
    """Tempo ocupado no dia: união dos horários (reuniões sobrepostas não contam duas vezes)."""
    spans = []
    for e in events:
        if e["all_day"] or not e["start"] or e["response"] == "declined":
            continue
        h, m = map(int, e["start"].split(":"))
        s = h * 60 + m
        if e["end"]:
            h, m = map(int, e["end"].split(":"))
            en = h * 60 + m
        else:
            en = s + 30
        spans.append((s, en if en > s else s + 30))
    total, cur = 0, None
    for s, en in sorted(spans):
        if cur is None or s > cur[1]:
            if cur:
                total += cur[1] - cur[0]
            cur = [s, en]
        else:
            cur[1] = max(cur[1], en)
    return total + (cur[1] - cur[0] if cur else 0)


def totals(days: list[date]) -> dict:
    per = {a.id: {"minutes": 0, "meetings": 0} for a in accounts}
    have = union = 0
    for d in days:
        data = read_cache(d)
        if data:
            have += 1
        # Reuniões marcadas como "não vou" saem da conta, como as recusadas
        skipped = plan.skipped_keys(d)
        data = {aid: {**rec, "events": [e for e in rec.get("events", []) if plan.event_key(aid, e) not in skipped]}
                for aid, rec in data.items()}
        # Total geral: reunião de duas empresas no mesmo horário conta uma vez só
        union += busy_minutes([e for a in accounts for e in data.get(a.id, {}).get("events", [])])
        for a in accounts:
            events = data.get(a.id, {}).get("events", [])
            per[a.id]["minutes"] += busy_minutes(events)
            per[a.id]["meetings"] += sum(1 for e in events if not e["all_day"] and e["start"] and e["response"] != "declined")
    return {"accounts": per, "busy_minutes": union, "days_read": have, "days_total": len(days)}


# ---------- Atualização automática ----------

def in_refresh_window(now: datetime) -> bool:
    if REFRESH_WEEKDAYS_ONLY and now.weekday() >= 5:
        return False
    return REFRESH_HOURS[0] <= now.hour < REFRESH_HOURS[1]


async def auto_refresh() -> None:
    last_month = 0.0
    last_prune = None
    first = True
    while True:
        # Na subida sempre lê, para o dashboard não abrir vazio
        if first or in_refresh_window(datetime.now()):
            try:
                today = date.today()
                if first or time.monotonic() - last_month > MONTH_REFRESH_SECONDS:
                    await refresh_range(*month_range(today))
                    last_month = time.monotonic()
                else:
                    await refresh_range(*week_of(today))
            except Exception:
                log.exception("atualização automática falhou")
        if last_prune != date.today():
            prune_cache()
            last_prune = date.today()
        first = False
        await asyncio.sleep(REFRESH_MINUTES * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await pool.start()
    task = asyncio.create_task(auto_refresh()) if REFRESH_MINUTES > 0 else None
    yield
    if task:
        task.cancel()
    await pool.stop()


app = FastAPI(title="ManaTime", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """O navegador sempre confere se o dashboard mudou (resposta 304 rápida quando não mudou)."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache"
    return response


def parse_day(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "data no formato YYYY-MM-DD")


def day_payload(day: date) -> dict:
    data = read_cache(day)
    return {
        "date": day.isoformat(),
        "plan": plan.for_day(day, today=date.today()),
        "accounts": [
            {"id": a.id, "name": a.name, "short": a.short, "provider": a.provider, "color": a.color,
             **data.get(a.id, {"status": "pending", "events": []})}
            for a in accounts
        ],
    }


def week_payload(day: date) -> dict:
    start, end = week_of(day)
    m_start, m_end = month_range(day)
    month_days = [d for d in days_between(m_start, m_end) if d.month == day.month]
    return {
        "start": start.isoformat(),
        "end": (end - timedelta(days=1)).isoformat(),
        "month": day.strftime("%Y-%m"),
        "days": [day_payload(d) for d in days_between(start, end)],
        "totals": {"week": totals(days_between(start, end)), "month": totals(month_days)},
    }


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def get_config():
    return {"novnc_port": NOVNC_PORT}


@app.get("/api/agenda")
async def get_agenda(day: str | None = Query(None, alias="date")):
    d = parse_day(day)
    await ensure(*week_of(d))
    return day_payload(d)


@app.get("/api/week")
async def get_week(day: str | None = Query(None, alias="date")):
    d = parse_day(day)
    await ensure(*week_of(d))
    return week_payload(d)


@app.post("/api/refresh")
async def post_refresh(day: str | None = Query(None, alias="date"), span: str = "week"):
    d = parse_day(day)
    if span not in ("week", "month"):
        raise HTTPException(400, "span deve ser week ou month")
    await refresh_range(*(month_range(d) if span == "month" else week_of(d)))
    return {"day": day_payload(d), "week": week_payload(d)}


@app.post("/api/accounts/{account_id}/login")
async def open_login(account_id: str):
    browser = pool.browsers.get(account_id)
    if not browser:
        raise HTTPException(404, "conta não encontrada")
    await browser.open_for_login(date.today())
    return {"ok": True}


# ---------- Plano: escolhas de conflito, atividades e tarefas ----------

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class SkipIn(BaseModel):
    date: Date
    keys: list[str] = Field(min_length=1, max_length=50)
    skip: bool


class ActivityIn(BaseModel):
    date: Date
    start: str = Field(pattern=HHMM)
    end: str = Field(pattern=HHMM)
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def strip_title(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("título vazio")
        return v.strip()

    @field_validator("end")
    @classmethod
    def end_after_start(cls, v: str, info) -> str:
        if "start" in info.data and v <= info.data["start"]:
            raise ValueError("o fim precisa ser depois do início")
        return v


class TaskIn(BaseModel):
    date: Date | None = None
    title: str = Field(min_length=1, max_length=200)
    time: str | None = Field(default=None, pattern=HHMM)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("title")
    @classmethod
    def strip_title(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("título vazio")
        return v.strip()


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    done: bool | None = None
    date: Date | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


@app.post("/api/plan/skip")
async def plan_skip(body: SkipIn):
    plan.set_skipped(body.date, body.keys, body.skip)
    return plan.for_day(body.date, today=date.today())


@app.post("/api/plan/activities")
async def plan_add_activity(body: ActivityIn):
    return plan.add_activity(body.date, body.start, body.end, body.title)


@app.delete("/api/plan/activities/{item_id}")
async def plan_delete_activity(item_id: str):
    if not plan.delete("activities", item_id):
        raise HTTPException(404, "atividade não encontrada")
    return {"ok": True}


@app.post("/api/plan/tasks")
async def plan_add_task(body: TaskIn):
    return plan.add_task(body.date, body.title, body.time, body.tags)


@app.patch("/api/plan/tasks/{task_id}")
async def plan_update_task(task_id: str, body: TaskPatch):
    if body.title is not None and not body.title.strip():
        raise HTTPException(422, "o título não pode ficar vazio")
    task = plan.update_task(task_id, title=body.title.strip() if body.title else None, done=body.done,
                            date=body.date.isoformat() if body.date else None, tags=body.tags)
    if task is None:
        raise HTTPException(404, "tarefa não encontrada")
    return task


@app.delete("/api/plan/tasks/{task_id}")
async def plan_delete_task(task_id: str):
    if not plan.delete("tasks", task_id):
        raise HTTPException(404, "tarefa não encontrada")
    return {"ok": True}


# ---------- Contas (aba Contas) ----------

HEX = r"^#[0-9a-fA-F]{6}$"


def account_public(a: Account) -> dict:
    return {"id": a.id, "name": a.name, "short": a.short, "provider": a.provider, "color": a.color, "url": a.url,
            "has_login": (PROFILES_DIR / a.id).exists()}


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    short: str = Field(min_length=1, max_length=4)
    provider: str
    color: str = Field(pattern=HEX)
    url: str | None = Field(default=None, max_length=500)

    @field_validator("name", "short")
    @classmethod
    def strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("campo vazio")
        return v.strip()

    @field_validator("provider")
    @classmethod
    def known_provider(cls, v: str) -> str:
        if v not in PROVIDERS:
            raise ValueError("tipo de conta desconhecido")
        return v

    # Validação do modelo inteiro: roda mesmo quando a url nem foi enviada
    @model_validator(mode="after")
    def custom_url(self):
        if self.provider == "custom":
            if not self.url or not self.url.startswith("https://") or "{d}" not in self.url:
                raise ValueError("para custom, use um endereço https:// com {y}, {m} e {d}")
        else:
            self.url = None
        return self


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    short: str | None = Field(default=None, min_length=1, max_length=4)
    color: str | None = Field(default=None, pattern=HEX)
    url: str | None = Field(default=None, max_length=500)


async def apply_accounts(new: list[Account]) -> None:
    """Grava, troca a lista em uso e ajusta os navegadores, sem disputar com uma leitura em andamento."""
    async with refresh_lock:
        save_accounts(new)
        accounts[:] = new
        await pool.sync(accounts)


def find(account_id: str) -> int:
    for i, a in enumerate(accounts):
        if a.id == account_id:
            return i
    raise HTTPException(404, "conta não encontrada")


@app.get("/api/accounts")
async def list_accounts():
    return {"accounts": [account_public(a) for a in accounts], "colors": ACCOUNT_COLORS, "providers": list(PROVIDERS)}


@app.post("/api/accounts")
async def add_account(body: AccountIn):
    base, n = slug(body.name), 2
    new_id = base
    # Não reaproveita o id de um perfil antigo no disco: a conta nova não pode herdar o login de outra
    taken = {a.id for a in accounts} | ({p.name for p in PROFILES_DIR.iterdir()} if PROFILES_DIR.exists() else set())
    while new_id in taken:
        new_id, n = f"{base}-{n}", n + 1
    account = Account(id=new_id, name=body.name, provider=body.provider, color=body.color.lower(),
                      short=body.short.upper()[:4], url=body.url)
    await apply_accounts([*accounts, account])
    return account_public(account)


@app.patch("/api/accounts/{account_id}")
async def edit_account(account_id: str, body: AccountPatch):
    i = find(account_id)
    old = accounts[i]
    url = old.url
    if body.url is not None and old.provider == "custom":
        if not body.url.startswith("https://") or "{d}" not in body.url:
            raise HTTPException(422, "para custom, use um endereço https:// com {y}, {m} e {d}")
        url = body.url
    updated = Account(id=old.id, provider=old.provider, url=url,
                      name=(body.name or old.name).strip(), color=(body.color or old.color).lower(),
                      short=(body.short or old.short).strip().upper()[:4])
    await apply_accounts([*accounts[:i], updated, *accounts[i + 1:]])
    return account_public(updated)


@app.post("/api/accounts/{account_id}/move")
async def move_account(account_id: str, step: int = Query(..., ge=-1, le=1)):
    i = find(account_id)
    j = i + step
    if 0 <= j < len(accounts) and step:
        new = list(accounts)
        new[i], new[j] = new[j], new[i]
        await apply_accounts(new)
    return {"accounts": [account_public(a) for a in accounts]}


@app.delete("/api/accounts/{account_id}")
async def remove_account(account_id: str, forget_login: bool = False):
    i = find(account_id)
    await apply_accounts([*accounts[:i], *accounts[i + 1:]])
    plan.forget_account(account_id)
    # O login salvo só some se você pedir: assim dá para recolocar a conta sem logar de novo
    if forget_login and (PROFILES_DIR / account_id).is_dir():
        shutil.rmtree(PROFILES_DIR / account_id, ignore_errors=True)
    return {"ok": True}


# ---------- To-do ----------

@app.get("/api/todo")
async def get_todo():
    return plan.todo(date.today())
