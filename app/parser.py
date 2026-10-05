"""Transforma o que o calendário mostra (texto de acessibilidade ou JSON da rede) em eventos.

Sem LLM: o Google e o Outlook descrevem cada evento para leitores de tela com título, horário,
local e status separados por vírgula, e o Outlook baixa os eventos em JSON com campos nomeados.
"""

import re
import unicodedata
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import TIMEZONE

LOCAL_TZ = ZoneInfo(TIMEZONE)

_T = r"(\d{1,2})(?:[:h](\d{2})|h)?\s*(am|pm|a\.m\.|p\.m\.)?"
TIME_RANGE = re.compile(rf"\b{_T}\s*(?:to|até|ate|para|a|às|as|–|—|-)\s*{_T}(?![\w/])", re.I)
SINGLE_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2})|h(\d{2})?|\s*(am|pm))(?![\w/])", re.I)

ALL_DAY = re.compile(r"^(all day|dia inteiro|o dia todo|dia todo|o dia inteiro)$", re.I)
LOCATION = re.compile(r"^(?:local|location|localização|localizacao)\s*:?\s+(.+)$", re.I)
ORGANIZER = re.compile(r"^(?:organizer|organizador|organizado por|organized by|por|by)\s*:?\s+(.+)$", re.I)

MONTHS = (
    "january february march april may june july august september october november december "
    "janeiro fevereiro março marco abril maio junho julho agosto setembro outubro novembro dezembro"
).split()
WEEKDAYS = (
    "monday tuesday wednesday thursday friday saturday sunday "
    "segunda-feira terça-feira terca-feira quarta-feira quinta-feira sexta-feira sábado sabado domingo"
).split()
DATE_WORD = re.compile(r"\b(" + "|".join(map(re.escape, MONTHS + WEEKDAYS)) + r")\b", re.I)

# Ordem importa: "aceito provisoriamente" é talvez, não aceito
RESPONSES = [
    ("declined", ("recusad", "recusou", "declined", "você recusou")),
    ("tentative", ("provisori", "provisóri", "tentative", "talvez", "maybe")),
    ("accepted", ("aceit", "accepted", "você é o organizador", "you are the organizer", "organizer", "organizador")),
    ("unknown", ("needs rsvp", "não respondeu", "nao respondeu", "aguardando", "precisa responder", "not responded")),
]
# Pedaços da descrição que não são título nem dado útil
NOISE = re.compile(
    r"^(recurring event|evento recorrente|recorrente|busy|ocupado|free|livre|private|particular|"
    r"working elsewhere|trabalhando em outro lugar|out of office|ausente|"
    r"fora do horário de trabalho|outside (of )?working hours|"
    r"nenhum local|no location|(agenda|calendar):.*|"
    r"exceção para evento recorrente|exception to recurring event|"
    r"no title|sem título|sem titulo|\(no title\)|\(sem título\)|"
    r"\d+ (guests?|convidados?)|has attachments?|tem anexos?|.*\b(reminder|lembrete)s?\b.*)$",
    re.I,
)


def _hhmm(h: str, m: str | None, mer: str | None) -> tuple[int, int, str | None]:
    return int(h), int(m or 0), (mer or "").lower().replace(".", "") or None


def _to_24h(h: int, mer: str | None) -> int:
    if mer == "am":
        return 0 if h == 12 else h
    if mer == "pm":
        return h if h == 12 else h + 12
    return h


def parse_time_range(text: str) -> tuple[str, str] | None:
    m = TIME_RANGE.search(text)
    if not m:
        return None
    h1, m1, mer1 = _hhmm(m.group(1), m.group(2), m.group(3))
    h2, m2, mer2 = _hhmm(m.group(4), m.group(5), m.group(6))
    # "10 a 11" sozinho não é horário; precisa de minutos, "h" ou am/pm em algum lado
    if not any([m.group(2), m.group(3), m.group(5), m.group(6)]) and not re.search(r"\dh", m.group(0), re.I):
        return None
    if mer1 is None and mer2:
        mer1 = mer2
        if _to_24h(h1, mer1) > _to_24h(h2, mer2):  # "11 to 12pm" = 11h às 12h
            mer1 = "am"
    s, e = _to_24h(h1, mer1), _to_24h(h2, mer2)
    if not (0 <= s < 24 and 0 <= e < 25 and m1 < 60 and m2 < 60):
        return None
    return f"{s:02d}:{m1:02d}", f"{e % 24:02d}:{m2:02d}"


def parse_single_time(text: str) -> str | None:
    m = SINGLE_TIME.search(text)
    if not m:
        return None
    h = _to_24h(int(m.group(1)), (m.group(4) or "").lower() or None)
    minute = int(m.group(2) or m.group(3) or 0)
    return f"{h:02d}:{minute:02d}" if h < 24 and minute < 60 else None


MONTH_NUM = {
    **{m: i + 1 for i, m in enumerate("january february march april may june july august september october november december".split())},
    **{m: i + 1 for i, m in enumerate("janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro".split())},
    "marco": 3,
}


def segment_date(segment: str) -> date | None:
    """'05 de Outubro de 2026', 'Segunda-feira, 5 de outubro de 2026' ou 'October 5, 2026'."""
    low = segment.lower()
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", low) or re.search(r"(\w+)\s+(\d{1,2}),?\s+(\d{4})", low)
    if not m:
        return None
    a, b, year = m.groups()
    day, month = (a, b) if a.isdigit() else (b, a)
    if month not in MONTH_NUM:
        return None
    try:
        return date(int(year), MONTH_NUM[month], int(day))
    except ValueError:
        return None


def _is_date(segment: str) -> bool:
    return bool(DATE_WORD.search(segment)) or bool(re.search(r"\b20\d\d\b", segment))


def _response(segment: str) -> str | None:
    low = segment.lower()
    for value, needles in RESPONSES:
        if any(n in low for n in needles):
            return value
    return None


def parse_description(text: str) -> dict | None:
    """Uma descrição de acessibilidade, ex.: '10:00 até 11:00, Daily, Aceito, Local: Sala 3, 4 de outubro de 2026'."""
    text = unicodedata.normalize("NFC", " ".join(text.split()))
    if not text:
        return None
    event = {"title": None, "start": None, "end": None, "all_day": False, "location": None,
             "meeting_url": None, "organizer": None, "response": "unknown", "date": None}
    found_time = False

    for raw in re.split(r",\s+|\s+·\s+|\n", text):
        seg = raw.strip(" .")
        if not seg:
            continue
        if not found_time and (rng := parse_time_range(seg)):
            event["start"], event["end"] = rng
            found_time = True
            # Outlook às vezes junta título e horário: "Daily 10:00 a 10:15"
            rest = TIME_RANGE.sub("", seg).strip(" ,-–")
            if rest and not _is_date(rest) and event["title"] is None and len(rest) > 1:
                event["title"] = rest
            continue
        if ALL_DAY.match(seg):
            event["all_day"] = True
            found_time = True
            continue
        if m := ORGANIZER.match(seg):
            event["organizer"] = m.group(1)
            if event["response"] == "unknown" and re.search(r"você|you", seg, re.I):
                event["response"] = "accepted"
            continue
        if m := LOCATION.match(seg):
            event["location"] = m.group(1)
            continue
        if NOISE.match(seg):
            continue
        if (resp := _response(seg)) and len(seg) < 60:
            if resp != "unknown":
                event["response"] = resp
            continue
        if _is_date(seg):
            event["date"] = event["date"] or segment_date(seg)
            continue
        if not found_time and (t := parse_single_time(seg)) and len(seg) <= 12:
            event["start"] = t
            found_time = True
            continue
        if event["title"] is None:
            event["title"] = seg

    if not found_time or not event["title"]:
        return None
    return event


# ---------- Outlook: JSON baixado pela própria página ----------

def _get(obj: dict, *names):
    """Primeiro campo presente, ignorando maiúsculas: _get(o, 'Subject') acha 'subject' também."""
    lower = {k.lower(): v for k, v in obj.items()}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _parse_dt(value) -> datetime | None:
    tz_name = None
    if isinstance(value, dict):
        tz_name = _get(value, "TimeZone")
        value = _get(value, "DateTime")
    if not isinstance(value, str) or not re.match(r"\d{4}-\d{2}-\d{2}T", value):
        return None
    value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        tz = ZoneInfo("UTC") if (tz_name or "").upper() in ("UTC", "ETC/UTC") else LOCAL_TZ
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(LOCAL_TZ)


def _text(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _text(_get(value, "DisplayName", "Name", "displayName"))
    return None


OUTLOOK_RESPONSES = {
    "accept": "accepted", "accepted": "accepted", "organizer": "accepted",
    "tentative": "tentative", "tentativelyaccepted": "tentative",
    "decline": "declined", "declined": "declined",
}


def outlook_event(obj: dict) -> dict | None:
    """Reconhece um evento no formato do OWA (Subject/Start/End) ou do Graph (subject/start/end)."""
    subject = _get(obj, "Subject")
    if not isinstance(subject, str):
        return None
    organizer = _get(obj, "Organizer")
    if not subject.strip() and not organizer:
        return None  # blocos de horário de trabalho vêm assim
    start, end = _parse_dt(_get(obj, "Start")), _parse_dt(_get(obj, "End"))
    if not start or not end:
        return None
    if _get(obj, "IsCancelled") is True:
        return None

    if isinstance(organizer, dict):
        organizer = _text(_get(organizer, "Mailbox", "EmailAddress")) or _text(organizer)
    response = _get(obj, "MyResponseType", "ResponseType")
    if isinstance(_get(obj, "ResponseStatus"), dict):
        response = _get(_get(obj, "ResponseStatus"), "Response")
    online = _get(obj, "OnlineMeeting")
    join = _get(obj, "OnlineMeetingJoinUrl", "OnlineMeetingUrl", "JoinUrl") or (
        _get(online, "JoinUrl") if isinstance(online, dict) else None)
    item_id = _get(obj, "ItemId", "Id")
    location = _text(_get(obj, "Location"))
    if not join and location and location.startswith("https://"):
        join = location  # convites com link do Meet/Zoom costumam pôr o link no local

    return {
        "id": _get(item_id, "Id") if isinstance(item_id, dict) else item_id,
        "title": subject.strip() or "(sem título)",
        "start_dt": start,
        "end_dt": end,
        "all_day": bool(_get(obj, "IsAllDayEvent", "IsAllDay")),
        "location": location,
        "meeting_url": join if isinstance(join, str) and join.startswith("https://") else None,
        "organizer": organizer if isinstance(organizer, str) else None,
        "response": OUTLOOK_RESPONSES.get(str(response or "").lower(), "unknown"),
    }


def find_outlook_events(payload, out: list, depth: int = 0) -> None:
    if depth > 40:
        return
    if isinstance(payload, dict):
        if (ev := outlook_event(payload)) is not None:
            out.append(ev)
            return
        for value in payload.values():
            find_outlook_events(value, out, depth + 1)
    elif isinstance(payload, list):
        for value in payload:
            find_outlook_events(value, out, depth + 1)


def outlook_events_for_day(raw: list[dict], day: date) -> list[dict]:
    day_start = datetime.combine(day, time.min, LOCAL_TZ)
    day_end = day_start + timedelta(days=1)
    events = {}
    for ev in raw:
        if not (ev["start_dt"] < day_end and ev["end_dt"] > day_start):
            continue
        key = ev["id"] or (ev["title"], ev["start_dt"])
        if key in events:  # o mesmo evento pode vir em mais de uma resposta; completa o que faltar
            for field, value in events[key].items():
                if value is None and field in ev and field not in ("start", "end"):
                    events[key][field] = ev[field]
            continue
        # Eventos de dia inteiro vêm com início/fim à meia-noite; eventos que atravessam o dia, idem
        whole_day = ev["all_day"] or (ev["start_dt"] <= day_start and ev["end_dt"] >= day_end)
        events[key] = {
            "title": ev["title"],
            "start": None if whole_day else max(ev["start_dt"], day_start).strftime("%H:%M"),
            "end": None if whole_day else (ev["end_dt"].strftime("%H:%M") if ev["end_dt"] < day_end else "23:59"),
            "all_day": whole_day,
            "location": ev["location"],
            "meeting_url": ev["meeting_url"],
            "organizer": ev["organizer"],
            "response": ev["response"],
        }
    return list(events.values())


# ---------- Montagem final ----------

COUNT = re.compile(r",\s*(\d+)\s+(?:eventos?|events?)\s*$", re.I)
FORWARD = re.compile(r"^\s*(?:enc|fw|fwd|res|re|tr|rv)\s*:\s*", re.I)
RESPONSE_RANK = {"accepted": 3, "tentative": 2, "unknown": 1, "declined": 0}


def expected_count(descriptions: list[str], first: date) -> int | None:
    """Total que o próprio calendário anuncia para a página.

    Google: 'Semana de 27 de setembro de 2026, 3 eventos' (semana) ou
    'segunda-feira, 28 de setembro de 2026, 1 evento' (dia).
    """
    for text in descriptions:
        m = COUNT.search(text)
        if m and segment_date(text) == first:
            return int(m.group(1))
    return None


def _base_title(title: str) -> str:
    while FORWARD.match(title):
        title = FORWARD.sub("", title, count=1)
    return " ".join(title.casefold().split())


def merge_forwarded(events: list[dict]) -> list[dict]:
    """'ENC: Retrô' e 'Retrô' no mesmo horário são o mesmo convite encaminhado: fica um só."""
    kept: dict[tuple, dict] = {}
    for ev in events:
        key = (_base_title(ev["title"]), ev["start"], ev["end"], ev["all_day"])
        other = kept.get(key)
        if other is None:
            kept[key] = ev
            continue
        best, rest = (ev, other) if RESPONSE_RANK[ev["response"]] > RESPONSE_RANK[other["response"]] else (other, ev)
        if FORWARD.match(best["title"]) and not FORWARD.match(rest["title"]):
            best = {**best, "title": rest["title"]}
        kept[key] = {k: best[k] if best[k] is not None else rest[k] for k in best}
    return list(kept.values())


def events_from_pages(pages: list[dict], start: date, end: date) -> tuple[dict, dict]:
    """Descrições lidas página a página -> ({dia: eventos}, {dia: eventos que faltaram ler})."""
    by_day = {d: {} for d in days_in(start, end)}
    missing = {}
    for page in pages:
        span = days_in(page["start"], page["end"])
        parsed = []
        for text in page["descriptions"]:
            ev = parse_description(text)
            if ev is None:
                continue
            if ev["date"] is None and len(span) == 1:
                ev["date"] = span[0]   # a página de um dia só pode omitir a data
            if ev["date"] in span:
                parsed.append(ev)
        unique = {(e["date"], e["title"], e["start"], e["end"]): e for e in parsed}
        expected = expected_count(page["descriptions"], page["start"])
        if expected is not None and len(unique) < expected:
            for d in span:
                if d in by_day:
                    missing[d] = expected - len(unique)
        for ev in unique.values():
            day = ev.pop("date")
            if day in by_day:
                by_day[day][(ev["title"], ev["start"], ev["end"])] = ev
    return {d: list(evs.values()) for d, evs in by_day.items()}, missing


def days_in(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days)]


def extract_range(start: date, end: date, snapshot: dict) -> tuple[dict, dict, str]:
    """({dia: eventos}, {dia: faltando}, origem). Se o calendário respondeu em JSON, confia nele."""
    if snapshot.get("json_seen"):
        by_day = {d: outlook_events_for_day(snapshot["json_events"], d) for d in days_in(start, end)}
        missing, source = {}, "json"
    else:
        by_day, missing = events_from_pages(snapshot["pages"], start, end)
        source = "dom"
    return {d: merge_forwarded(evs) for d, evs in by_day.items()}, missing, source


def extract_events(day: date, snapshot: dict) -> tuple[list[dict], str]:
    """Um dia só; usado pelos testes e por quem tem um snapshot de uma página."""
    snap = dict(snapshot)
    snap.setdefault("pages", [{"start": day, "end": day + timedelta(days=1), "descriptions": snapshot.get("descriptions", [])}])
    by_day, _, source = extract_range(day, day + timedelta(days=1), snap)
    return by_day[day], source
