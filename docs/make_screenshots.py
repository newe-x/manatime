"""Gera as capturas do README com dados fictícios (tests/ui/fake_api.py), sem nenhuma agenda real.

    pip install -r requirements-dev.txt && playwright install chromium
    python docs/make_screenshots.py
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "ui"))
from fake_api import URL, install  # noqa: E402

OUT = ROOT / "docs" / "img"
SHOTS = [  # nome, aba, tema, largura, altura, celular
    ("dia", "day", "light", 1280, 1100, False),
    ("semana", "week", "light", 1280, 1180, False),
    ("todo", "todo", "light", 1280, 820, False),
    ("contas", "accounts", "light", 1280, 980, False),
    ("dia-preto", "day", "dark", 1280, 1100, False),
    ("celular", "day", "light", 390, 844, True),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    for name, view, theme, w, h, mobile in SHOTS:
        ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2 if mobile else 1,
                                  is_mobile=mobile, has_touch=mobile, locale="pt-BR", timezone_id="America/Sao_Paulo")
        page = ctx.new_page()
        install(page)
        page.add_init_script(f"localStorage.setItem('agenda.view', '{view}'); localStorage.setItem('mana.theme', '{theme}');")
        page.goto(URL)
        page.wait_for_selector("#title:not(:text('Agenda'))")
        page.wait_for_timeout(600)
        page.screenshot(path=OUT / f"{name}.png", full_page=False)
        ctx.close()
        print("ok", name)
    browser.close()
