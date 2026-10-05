import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "accounts.yaml"
    cfg.write_text("accounts:\n  - {id: a, name: Empresa A, short: AAA, provider: outlook, color: '#a276e4'}\n")
    monkeypatch.setenv("ACCOUNTS_FILE", str(cfg))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REFRESH_MINUTES", "0")
    import app.config, app.plan
    importlib.reload(app.config); importlib.reload(app.plan)
    import app.browser
    importlib.reload(app.browser)
    import app.main
    main = importlib.reload(app.main)
    with TestClient(main.app) as c:
        yield c, main, cfg


def test_add_edit_move_remove(client):
    c, main, cfg = client
    r = c.post("/api/accounts", json={"name": "Nova Empresa", "short": "nov", "provider": "google", "color": "#2468C9"})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["id"] == "nova-empresa" and new["short"] == "NOV" and new["color"] == "#2468c9"
    assert "nova-empresa" in main.pool.browsers and "Nova Empresa" in cfg.read_text()

    assert c.patch("/api/accounts/nova-empresa", json={"name": "Renomeada", "color": "#d92d20"}).json()["name"] == "Renomeada"
    assert main.pool.browsers["nova-empresa"].account.name == "Renomeada"

    order = c.post("/api/accounts/nova-empresa/move?step=-1").json()["accounts"]
    assert [a["id"] for a in order] == ["nova-empresa", "a"]

    assert c.delete("/api/accounts/nova-empresa").json() == {"ok": True}
    assert [a["id"] for a in c.get("/api/accounts").json()["accounts"]] == ["a"]
    assert "nova-empresa" not in main.pool.browsers and "Renomeada" not in cfg.read_text()


def test_validation(client):
    c, _, _ = client
    assert c.post("/api/accounts", json={"name": "X", "short": "X", "provider": "yahoo", "color": "#000000"}).status_code == 422
    assert c.post("/api/accounts", json={"name": "X", "short": "X", "provider": "custom", "color": "#000000"}).status_code == 422
    assert c.post("/api/accounts", json={"name": " ", "short": "X", "provider": "google", "color": "#000000"}).status_code == 422
    assert c.delete("/api/accounts/nao-existe").status_code == 404


def test_id_never_reuses_old_profile(client, tmp_path):
    c, _, _ = client
    (tmp_path / "profiles" / "empresa-b").mkdir(parents=True)
    assert c.post("/api/accounts", json={"name": "Empresa B", "short": "B", "provider": "outlook", "color": "#a276e4"}).json()["id"] == "empresa-b-2"


def test_task_title_edit(client):
    c, _, _ = client
    t = c.post("/api/plan/tasks", json={"title": "Rascunho", "tags": ["a"]}).json()
    r = c.patch(f"/api/plan/tasks/{t['id']}", json={"title": "  Versão final  ", "tags": ["a", "b"]})
    assert r.json()["title"] == "Versão final" and r.json()["tags"] == ["a", "b"]
    assert c.patch(f"/api/plan/tasks/{t['id']}", json={"title": "   "}).status_code == 422


def test_starts_without_accounts_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNTS_FILE", str(tmp_path / "nao-existe.yaml"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REFRESH_MINUTES", "0")
    import app.config, app.plan, app.browser, app.main
    for m in (app.config, app.plan, app.browser):
        importlib.reload(m)
    main = importlib.reload(app.main)
    with TestClient(main.app) as c:
        assert c.get("/api/accounts").json()["accounts"] == []
        assert c.post("/api/accounts", json={"name": "Acme", "short": "ACM", "provider": "google", "color": "#a276e4"}).status_code == 200
        assert "Acme" in (tmp_path / "nao-existe.yaml").read_text()
