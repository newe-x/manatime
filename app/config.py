import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import yaml

CONFIG_FILE = Path(os.environ.get("ACCOUNTS_FILE", "/config/accounts.yaml"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PROFILES_DIR = DATA_DIR / "profiles"
CACHE_DIR = DATA_DIR / "cache"
DEBUG_DIR = DATA_DIR / "debug"
REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "15"))
# Atualização automática só nesse intervalo de horas ("7-20" = das 7h até 19h59)
REFRESH_HOURS = tuple(int(h) for h in os.environ.get("REFRESH_HOURS", "7-20").split("-"))
REFRESH_WEEKDAYS_ONLY = os.environ.get("REFRESH_WEEKDAYS_ONLY", "true").lower() in ("1", "true", "yes", "sim")
NOVNC_PORT = int(os.environ.get("NOVNC_PORT", "6080"))
# Dias de cache guardados; o mês anterior inteiro continua disponível para as horas por empresa
CACHE_DAYS = int(os.environ.get("CACHE_DAYS", "100"))
# Segundos sem uso até o Chromium de uma conta ser fechado (ele reabre na próxima leitura)
BROWSER_IDLE_SECONDS = int(os.environ.get("BROWSER_IDLE_SECONDS", "90"))
TIMEZONE = os.environ.get("TZ", "America/Sao_Paulo")

DAY_URLS = {
    # Visão da semana: cada evento vem descrito com a data, então uma página cobre 7 dias
    "google": "https://calendar.google.com/calendar/u/0/r/week/{y}/{m}/{d}",
    # O Outlook ignora datas na URL; o dia é escolhido reescrevendo o GetCalendarView (browser.py)
    "outlook": "https://outlook.office.com/calendar/view/day",
    "outlook_live": "https://outlook.live.com/calendar/0/view/day",
}

# Se o Chromium cair numa dessas páginas, a sessão expirou
LOGIN_HOSTS = (
    "accounts.google.com",
    "login.microsoftonline.com",
    "login.microsoft.com",
    "login.live.com",
    "workspace.google.com",  # Google manda para a página do produto quando não há sessão
)


def week_start(day: date) -> date:
    """Domingo da semana do dia: é onde o Google e o Outlook começam a semana em pt-BR."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def month_range(day: date) -> tuple[date, date]:
    """Semanas inteiras que cobrem o mês do dia (fim exclusivo)."""
    first = day.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    end = week_start(next_month - timedelta(days=1)) + timedelta(days=7)
    return week_start(first), end


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days)]


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    provider: str
    color: str
    short: str
    url: str | None = None

    @property
    def by_week(self) -> bool:
        """Google lê uma semana por página; Outlook lê qualquer intervalo num pedido; custom, um dia por página."""
        return self.provider == "google"

    def day_url(self, day: date) -> str:
        template = self.url if self.provider == "custom" else DAY_URLS[self.provider]
        return template.format(y=day.year, m=day.month, d=day.day)

    def login_url(self, day: date) -> str:
        # Sem sessão, o Google Agenda cai na página de marketing; vai direto ao login
        if self.provider == "google":
            return "https://accounts.google.com/ServiceLogin?service=cl&continue=" + quote(self.day_url(day), safe="")
        return self.day_url(day)


PROVIDERS = ("google", "outlook", "outlook_live", "custom")
# Cores validadas juntas (daltonismo e contraste, temas claro e preto); evitam as da marca
# (verde-água = você, laranja = conflito, azul-marinho = interface)
ACCOUNT_COLORS = ["#a276e4", "#d92d20", "#2468c9", "#b88a0a", "#d6409f", "#5f8a1f"]


def slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:30] or "conta"


def save_accounts(accounts: list[Account]) -> None:
    """Grava a lista no accounts.yaml (troca atômica: um erro no meio não deixa o arquivo pela metade)."""
    items = []
    for a in accounts:
        item = {"id": a.id, "name": a.name, "short": a.short, "provider": a.provider, "color": a.color}
        if a.url:
            item["url"] = a.url
        items.append(item)
    header = ("# Contas da ManaTime. Editável pela aba Contas do dashboard.\n"
              "# provider: google | outlook (Microsoft 365) | outlook_live (Outlook pessoal) | custom (precisa de url com {y}, {m}, {d})\n"
              "# Cada conta tem seu próprio perfil do Chromium (data/profiles/<id>).\n")
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(header + yaml.safe_dump({"accounts": items}, allow_unicode=True, sort_keys=False))
    os.replace(tmp, CONFIG_FILE)


def load_accounts() -> list[Account]:
    # Sem arquivo ainda: começa sem contas; a aba Contas cria o accounts.yaml na primeira inclusão
    if not CONFIG_FILE.exists():
        return []
    raw = (yaml.safe_load(CONFIG_FILE.read_text()) or {}).get("accounts") or []
    accounts = []
    for item in raw:
        provider = item["provider"]
        if provider not in DAY_URLS and provider != "custom":
            raise ValueError(f"provider desconhecido em {item['id']}: {provider}")
        if provider == "custom" and not item.get("url"):
            raise ValueError(f"conta {item['id']} é custom e precisa de url")
        accounts.append(
            Account(
                id=item["id"],
                name=item.get("name", item["id"]),
                provider=provider,
                color=item.get("color", "#64748b"),
                short=str(item.get("short") or item.get("name", item["id"])[:3]).upper()[:4],
                url=item.get("url"),
            )
        )
    return accounts
