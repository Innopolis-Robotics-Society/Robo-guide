"""Optional reference UI resources; not required by robot control API."""

from pathlib import Path

from aiohttp import web

from .promo import PromoStore
from .state import TourState


def register(app, cfg, api):
    """Register optional static assets, preferences and promotion media."""
    installed_root = Path(__file__).resolve().parents[1] / "reference_ui/web"
    root = (
        Path(cfg.web_dir)
        if cfg.web_dir
        else (
            installed_root
            if installed_root.is_dir()
            else Path(__file__).resolve().parents[3] / "reference_ui/web"
        )
    )
    if not cfg.serve_ui or not root.is_dir():
        return
    promo = PromoStore(Path(cfg.promo_dir), cfg.promo_interval_s)
    prefs = TourState(cfg.state_dir, cfg.default_tour)

    async def index(request):
        return web.FileResponse(root / "index.html")

    async def config(request):
        return web.json_response(
            {
                "always_promo": cfg.always_promo,
                "slide_interval_s": cfg.slide_interval_s,
                "promo_interval_s": cfg.promo_interval_s,
                "stack_control": cfg.stack_control,
                "visitor_confirm_buttons": cfg.visitor_confirm_buttons,
            }
        )

    async def preferences(request):
        if request.method == "POST":
            data = await request.json()
            from .validation import object_fields

            object_fields(data, ("tour_id", "confirm_between_stops"))
            if "tour_id" in data:
                from .state import valid_tour_id

                if not valid_tour_id(data["tour_id"]):
                    raise ValueError("Invalid tour_id")
            if "confirm_between_stops" in data:
                if not isinstance(data["confirm_between_stops"], bool):
                    raise ValueError("Invalid confirm_between_stops")
            prefs.update(data)
        return web.json_response(
            {"tour_id": prefs.current()[0], "confirm_between_stops": prefs.confirm_between_stops()}
        )

    async def manifest(request):
        return web.json_response(promo.manifest())

    async def media(request):
        root = promo.media_root.resolve()
        file = (root / request.match_info["tail"]).resolve()
        if root not in file.parents or not file.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(file)

    app.router.add_get("/", index)
    app.router.add_static("/static", root)
    app.router.add_get("/api/v1/reference/config", config)
    app.router.add_get("/api/v1/reference/preferences", preferences)
    app.router.add_post("/api/v1/reference/preferences", api._authed(preferences))
    app.router.add_get("/api/v1/reference/promo", manifest)
    app.router.add_get("/api/v1/reference/promo-files/{tail:.+}", media)
