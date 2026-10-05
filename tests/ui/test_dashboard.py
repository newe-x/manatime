"""Testes de fumaça da interface: cada aba abre, mostra só o que deve e os fluxos principais funcionam.

Rodam contra a API falsa (fake_api.py), sem Docker nem agendas reais:
    pip install -r requirements-dev.txt && playwright install chromium
    pytest tests/ui
"""
from fake_api import URL, install

VIEWS = ["day", "week", "todo", "accounts"]


def open_dashboard(page, view="day"):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    api = install(page)
    page.add_init_script(f"localStorage.setItem('agenda.view', '{view}')")
    page.goto(URL)
    page.wait_for_selector("#title:not(:text('Agenda'))")
    return api, errors


def visible_views(page):
    return [v for v in ("dayview", "weekview", "todoview", "accountsview")
            if page.evaluate(f"getComputedStyle(document.getElementById('{v}')).display") != "none"]


def test_each_tab_shows_only_its_section(page):
    _, errors = open_dashboard(page)
    expected = {"day": "dayview", "week": "weekview", "todo": "todoview", "accounts": "accountsview"}
    for v in VIEWS:
        page.click(f"#view-{v}")
        page.wait_for_function(f"view === '{v}'")
        page.wait_for_timeout(150)
        assert visible_views(page) == [expected[v]], v
    assert errors == []


def test_tabs_do_not_jump_between_sections(page):
    open_dashboard(page)
    x = page.locator("#view-day").bounding_box()["x"]
    page.click("#view-todo")
    page.wait_for_function("view === 'todo'")
    assert page.locator("#view-day").bounding_box()["x"] == x


def test_day_line_has_free_time_tasks_and_now(page):
    open_dashboard(page)
    page.wait_for_selector(".nowrow")
    assert page.locator(".gap").count() >= 1                      # tempo livre com "+ atividade"
    assert page.locator(".ev.task-ev").inner_text().startswith("11:30")   # tarefa com horário no trilho
    assert "você não vai" in page.locator(".ev.skipped").inner_text()
    assert page.locator("#ribbon .axis span").first.inner_text() == "0h"   # dia inteiro


def test_choose_meeting_in_conflict(page):
    api, _ = open_dashboard(page)
    # 14:15 Acme × 14:00 Initech: fico na da Acme, a outra vira "você não vai"
    page.locator(".ev", has_text="Revisão de contrato").locator("button.choose").click()
    page.wait_for_selector(".ev.skipped:has-text('1:1 com gestão')")
    assert "initech|14:00|1:1 com gestão" in api.skipped["2026-10-05"]


def test_week_calendar_first_and_skipped_transparent(page):
    open_dashboard(page, "week")
    page.wait_for_selector(".wday")
    assert page.evaluate("document.getElementById('weekview').firstElementChild.id") == "week"
    skipped = page.locator(".wev.declined").first
    assert float(skipped.evaluate("e => getComputedStyle(e).opacity")) < 0.5


def test_todo_tags_filter_and_edit(page):
    api, errors = open_dashboard(page, "todo")
    page.fill("#todo-title", "Preparar apresentação #acme #slides")
    page.press("#todo-title", "Enter")
    page.wait_for_selector(".titem:has-text('Preparar apresentação')")
    assert api.tasks[-1]["tags"] == ["acme", "slides"]
    page.click("#tagbar [data-tagfilter='acme']")
    assert set(page.locator(".titem label").all_inner_texts()) >= {"Preparar apresentação", "Enviar proposta revisada\nhoje · 11:30"} or \
        all("acme" in t for t in page.locator(".titem").all_inner_texts())
    item = page.locator(".titem", has_text="Preparar apresentação")
    item.locator("[data-edittags]").click()
    page.fill("[data-tagform] input", "Apresentação final #acme")
    page.press("[data-tagform] input", "Enter")
    page.wait_for_selector(".titem:has-text('Apresentação final')")
    assert api.tasks[-1]["title"] == "Apresentação final" and api.tasks[-1]["tags"] == ["acme"]
    assert errors == []


def test_pomodoro_runs_pauses_and_finishes(page):
    open_dashboard(page, "todo")
    page.click(".pomo-cfg summary")
    page.fill("#pomo-form input[name=focus]", "1")
    page.click("#pomo-form button[type=submit]")
    assert page.inner_text("#pomo-time") == "01:00"
    page.click("#pomo-start")
    page.wait_for_function("document.getElementById('pomo-time').textContent !== '01:00'")
    assert "Foco" in page.title()
    page.evaluate("pst.endsAt = Date.now() - 1; pomoSave()")
    page.wait_for_function("document.getElementById('pomo-mode').textContent === 'Pausa curta'")
    assert page.inner_text("#pomo-stats").startswith("1 pomodoro concluído")


def test_pomodoro_finished_while_closed_shows_note(page):
    page.add_init_script("""localStorage.setItem('mana.pomo.state', JSON.stringify(
      {mode:'focus', running:true, endsAt: Date.now() - 600000, left: 0, done: 0, taskId: null, taskTitle: null}))""")
    open_dashboard(page, "todo")
    page.wait_for_selector("#pomo-task .late")
    assert "com a aba fechada" in page.inner_text("#pomo-task")
    assert page.inner_text("#pomo-start").strip() == "Iniciar"     # não começa a pausa sozinho, atrasado


def test_accounts_list_and_add_form(page):
    open_dashboard(page, "accounts")
    page.wait_for_selector(".acc-row")
    assert page.locator(".acc-row").count() == 3
    page.fill("#acc-add input[name=name]", "Umbrella")
    assert page.input_value("#acc-add input[name=short]") == "UMB"


def test_black_theme_persists(page):
    open_dashboard(page)
    page.click("#theme")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
