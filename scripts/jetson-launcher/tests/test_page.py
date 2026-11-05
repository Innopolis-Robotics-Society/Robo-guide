"""Настоящая страница web/: отдаётся сервером, ids сходятся, все её эндпоинты есть на сервере."""

from __future__ import annotations

import re
from pathlib import Path

from test_server import launcher, run

WEB = Path(__file__).resolve().parents[3] / "reference_ui" / "web"


def _js() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


def _html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


def test_real_page_is_served_without_cache(tmp_path):
    async def go():
        async with launcher(tmp_path, web_dir=str(WEB)) as (c, *_):
            r = await c.get("/")
            assert r.status == 200
            assert "no-store" in r.headers["Cache-Control"]
            html = await r.text()
            assert '<script src="/static/app.js">' in html
            assert "Начать экскурсию" in html
            for name in ("app.js", "app.css"):
                r = await c.get(f"/static/{name}")
                assert r.status == 200
                assert await r.read() == (WEB / name).read_bytes()
                assert "no-store" in r.headers["Cache-Control"]

    run(go())


def test_every_element_id_used_by_the_script_exists_in_html():
    html_ids = set(re.findall(r'\bid="([^"]+)"', _html()))
    used = set(re.findall(r'getElementById\("([^"]+)"\)', _js()))
    assert used, "app.js не обращается к элементам?"
    assert used <= html_ids, f"нет в index.html: {sorted(used - html_ids)}"


def test_page_has_no_removed_auth_leftovers():
    js = _js()
    assert "Authorization" not in js and "Bearer" not in js
    assert "auth-mock-badge" not in _html() and "mock" not in js.lower()
    assert "localStorage" not in js and "sessionStorage" not in js


def test_every_endpoint_of_the_page_exists_on_the_server(tmp_path):
    async def go():
        async with launcher(tmp_path) as (c, *_):
            for method, path in re.findall(r'request\("(GET|POST)", "([^"`]+)"', _js()):
                response = await c.request(method, path, json={} if method == "POST" else None)
                assert response.status not in (404, 405), (method, path)

    run(go())
