"""Настоящая страница web/: отдаётся сервером, ids сходятся, все её эндпоинты есть на сервере."""

from __future__ import annotations

import re
from pathlib import Path

from aiohttp.test_utils import make_mocked_request
from guide_launcher.bridge import is_proxy_allowed
from test_server import launcher, run

WEB = Path(__file__).resolve().parent.parent / "web"


def _js() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


def _html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


def test_real_page_is_served_without_cache(tmp_path):
    async def go():
        async with launcher(tmp_path, web_dir=str(WEB)) as (c, *_):
            r = await c.get("/")
            assert r.status == 200
            assert "no-cache" in r.headers["Cache-Control"]
            html = await r.text()
            assert '<script src="/static/app.js">' in html
            assert "Начать экскурсию" in html
            for name in ("app.js", "app.css"):
                r = await c.get(f"/static/{name}")
                assert r.status == 200
                assert await r.read() == (WEB / name).read_bytes()
                assert "no-cache" in r.headers["Cache-Control"]

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
    js = re.sub(r"(?m)(^|\s)//.*$", "", _js())  # пути в комментариях -- не вызовы
    js = re.sub(r"\$\{[^}]*\}", "~", js)
    paths = set(re.findall(r"(?<![\w.])(/(?:api|ros|promo)(?:/[\w.\-]+)*)", js))
    assert {"/api/config", "/api/stack/status", "/api/public/start_tour", "/ros/ws"} <= paths

    async def go():
        async with launcher(tmp_path, web_dir=str(WEB)) as (c, *_):
            canonical = {r.canonical for r in c.app.router.resources()}
            missing = []
            for path in sorted(paths):
                if path.startswith("/api/"):
                    ok = path in canonical
                elif path.startswith("/ros/"):
                    # /ros/media -- базовый URL: к нему страница дописывает /<файл>
                    tail = path[len("/ros/") :]
                    ok = is_proxy_allowed(tail) or is_proxy_allowed(f"{tail}/x")
                else:
                    match = await c.app.router.resolve(make_mocked_request("GET", f"{path}/x"))
                    ok = match.http_exception is None
                if not ok:
                    missing.append(path)
            assert not missing, f"страница ходит в несуществующие пути: {missing}"

    run(go())
