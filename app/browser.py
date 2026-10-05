"""Um Chromium com perfil persistente por conta, rodando na tela virtual do container."""

import asyncio
import json
import logging
import time
from datetime import date, timedelta
from urllib.parse import quote, unquote, urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import BrowserContext, Page, Playwright, Request, Response, Route, async_playwright

from .config import BROWSER_IDLE_SECONDS, LOGIN_HOSTS, PROFILES_DIR, TIMEZONE, Account, week_start
from .parser import find_outlook_events

log = logging.getLogger(__name__)

DISK_CACHE_BYTES = 40 * 1024 * 1024
LOGIN_KEEP_OPEN_SECONDS = 30 * 60  # tempo para fazer login (com MFA) antes de o Chromium fechar sozinho
OUTLOOK_VIEW = "**/service.svc?action=GetCalendarView*"

# Descrições de acessibilidade dos eventos na visão do dia.
# Google: cada evento é um [data-eventid] com uma descrição escondida para leitores de tela.
# Outlook e outros: elementos com aria-label que contém vírgulas e números (horário/data).
DESCRIPTIONS_JS = r"""
() => {
  const out = [];
  const main = document.querySelector('[role="main"]') || document.body;
  for (const chip of main.querySelectorAll('[data-eventid]')) {
    const label = chip.getAttribute('aria-label');
    if (label) out.push(label);
    const leaves = [...chip.querySelectorAll('*')]
      .filter((el) => el.children.length === 0)
      .map((el) => (el.textContent || '').trim())
      .filter(Boolean);
    const richest = leaves.reduce((best, t) => (t.split(',').length > best.split(',').length ? t : best), '');
    if (richest.includes(',')) out.push(richest);
    else if (leaves.length) out.push(leaves.join(', '));
  }
  for (const el of main.querySelectorAll('[aria-label]')) {
    const t = el.getAttribute('aria-label').trim();
    if (t.length > 12 && t.length < 800 && t.includes(',') && /\d/.test(t)) out.push(t);
  }
  return [...new Set(out)];
}
"""


class AccountBrowser:
    """Chromium de uma conta, aberto só enquanto está sendo usado.

    O perfil fica no disco, então fechar não desloga: a próxima leitura reabre com a mesma sessão.
    """

    def __init__(self, account: Account, pw: Playwright):
        self.account = account
        self.pw = pw
        self.context: BrowserContext | None = None
        self.lock = asyncio.Lock()
        self.keep_open_until = 0.0
        self._idle: asyncio.Task | None = None

    async def launch(self) -> None:
        profile = PROFILES_DIR / self.account.id
        profile.mkdir(parents=True, exist_ok=True)
        for leftover in profile.glob("Singleton*"):
            leftover.unlink(missing_ok=True)
        self.context = await self.pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,  # Google e Microsoft bloqueiam login em headless com mais frequência
            viewport=None,
            locale="pt-BR",
            timezone_id=TIMEZONE,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-position=0,0",
                "--window-size=1440,900",
                "--hide-crash-restore-bubble",
                f"--window-name={self.account.name}",
                f"--disk-cache-size={DISK_CACHE_BYTES}",
            ],
            ignore_default_args=["--enable-automation"],
        )
        # Fechar a janela no noVNC encerra o Chromium; o próximo uso abre de novo com o mesmo perfil
        self.context.on("close", self._on_close)
        log.info("Chromium aberto para %s", self.account.id)

    def _on_close(self, _) -> None:
        self.context = None

    async def close(self) -> None:
        if self.context:
            context, self.context = self.context, None
            await context.close()
            log.info("Chromium fechado para %s", self.account.id)

    def _schedule_close(self) -> None:
        if self._idle:
            self._idle.cancel()
        self._idle = asyncio.create_task(self._close_when_idle())

    async def _close_when_idle(self) -> None:
        while True:
            wait = max(BROWSER_IDLE_SECONDS, self.keep_open_until - time.monotonic())
            await asyncio.sleep(wait)
            if time.monotonic() >= self.keep_open_until and not self.lock.locked():
                await self.close()
                return

    async def page(self) -> Page:
        if self.context is None:
            await self.launch()
        if self.context.pages:
            return self.context.pages[0]
        return await self.context.new_page()

    async def open_for_login(self, day: date) -> None:
        async with self.lock:
            self.keep_open_until = time.monotonic() + LOGIN_KEEP_OPEN_SECONDS
            page = await self.page()
            await page.bring_to_front()
            await page.goto(self.account.login_url(day), wait_until="domcontentloaded")
        self._schedule_close()

    async def snapshot(self, start: date, end: date) -> dict:
        """Lê os eventos de [start, end): JSON da rede (Outlook) e descrições das páginas (Google e outros)."""
        async with self.lock:
            try:
                return await self._snapshot(start, end)
            finally:
                self._schedule_close()

    async def _snapshot(self, start: date, end: date) -> dict:
        page = await self.page()
        state = {"json_events": [], "json_seen": False, "pending": []}

        async def read_json(response: Response) -> None:
            try:
                find_outlook_events(await response.json(), state["json_events"])
                state["json_seen"] = True
            except Exception:
                log.warning("%s: resposta do calendário ilegível (%s)", self.account.id, response.status)

        def on_response(response: Response) -> None:
            if "action=GetCalendarView" in response.url and response.ok:
                state["pending"].append(asyncio.create_task(read_json(response)))

        # O Outlook ignora a data da URL e sempre carrega a semana atual.
        # Trocamos o intervalo do pedido que ele mesmo faz pelo que queremos.
        async def rewrite_range(route: Route, request: Request) -> None:
            headers = await request.all_headers()
            try:
                body = json.loads(unquote(headers["x-owa-urlpostdata"]))
                body["Body"]["RangeStart"] = f"{start.isoformat()}T00:00:00.000"
                body["Body"]["RangeEnd"] = f"{end.isoformat()}T00:00:00.000"
            except (KeyError, ValueError, TypeError):
                log.warning("%s: formato do GetCalendarView mudou; usando o pedido original", self.account.id)
                await route.continue_()
                return
            headers = {k: v for k, v in headers.items() if not k.startswith(":")}
            headers["x-owa-urlpostdata"] = quote(json.dumps(body, separators=(",", ":")))
            await route.continue_(headers=headers)

        is_outlook = self.account.provider in ("outlook", "outlook_live")
        if is_outlook:
            starts = [start]
        elif self.account.by_week:
            starts = list(range_step(week_start(start), end, 7))
        else:
            starts = list(range_step(start, end, 1))

        page.on("response", on_response)
        if is_outlook:
            await page.route(OUTLOOK_VIEW, rewrite_range)
        try:
            pages = []
            for first in starts:
                loaded = await self._load(page, first)
                if loaded is None:
                    return {"login_required": True}
                span = 7 if self.account.by_week else 1
                pages.append({"start": first, "end": first + timedelta(days=span), "descriptions": loaded})
            await asyncio.gather(*state["pending"])
        finally:
            page.remove_listener("response", on_response)
            if is_outlook:
                await page.unroute(OUTLOOK_VIEW, rewrite_range)
        return {
            "login_required": False,
            "url": page.url,
            "pages": [] if is_outlook else pages,
            "descriptions": [d for p in pages for d in p["descriptions"]],
            "json_events": state["json_events"],
            "json_seen": state["json_seen"],
        }

    async def _load(self, page: Page, first: date) -> list[str] | None:
        """Abre a página do calendário em `first`. Devolve as descrições, ou None se pediu login."""
        await page.goto(self.account.day_url(first), wait_until="domcontentloaded", timeout=60_000)
        if self._on_login_page(page):
            return None
        try:
            await page.wait_for_selector('[role="main"]', timeout=25_000)
        except PlaywrightTimeout:
            if self._on_login_page(page):
                return None
            raise RuntimeError(f"calendário não carregou (página atual: {page.url})")
        # Os calendários carregam os eventos depois do layout; esperar a rede acalmar
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeout:
            pass  # Outlook e Google mantêm conexões abertas; seguimos com o que chegou
        await page.wait_for_timeout(1_500)
        if self._on_login_page(page):
            return None
        return await page.evaluate(DESCRIPTIONS_JS)

    @staticmethod
    def _on_login_page(page: Page) -> bool:
        return urlparse(page.url).hostname in LOGIN_HOSTS


def range_step(start: date, end: date, step: int):
    day = start
    while day < end:
        yield day
        day += timedelta(days=step)


class BrowserPool:
    def __init__(self, accounts: list[Account]):
        self.accounts = accounts
        self.browsers: dict[str, AccountBrowser] = {}
        self._pw: Playwright | None = None

    async def start(self) -> None:
        # Nenhum Chromium abre aqui: cada conta abre o seu na primeira leitura
        self._pw = await async_playwright().start()
        self.browsers = {a.id: AccountBrowser(a, self._pw) for a in self.accounts}

    async def sync(self, accounts: list[Account]) -> None:
        """Acompanha mudanças na aba Contas: fecha o Chromium das removidas, cria o das novas."""
        self.accounts = accounts
        wanted = {a.id: a for a in accounts}
        for account_id in list(self.browsers):
            if account_id not in wanted:
                await self.browsers.pop(account_id).close()
        for account in accounts:
            if account.id in self.browsers:
                self.browsers[account.id].account = account   # nome, cor ou endereço podem ter mudado
            else:
                self.browsers[account.id] = AccountBrowser(account, self._pw)

    async def stop(self) -> None:
        for browser in self.browsers.values():
            await browser.close()
        if self._pw:
            await self._pw.stop()
