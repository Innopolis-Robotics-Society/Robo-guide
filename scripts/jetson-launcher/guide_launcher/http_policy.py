"""Uniform errors and explicit loopback origin policy."""

import uuid
from urllib.parse import urlsplit

from aiohttp import web


@web.middleware
async def policy(request, handler):
    """Apply origin checks and normalize errors without exposing internal exceptions."""
    request_id = str(uuid.uuid4())
    request["request_id"] = request_id
    origin = request.headers.get("Origin")
    same_origin = f"{request.scheme}://{request.host}"
    allowed = request.app["cfg"].allowed_origins
    permitted = not origin or origin == same_origin or origin in allowed
    try:
        if urlsplit(same_origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise web.HTTPForbidden(reason="host_not_allowed")
        if not permitted:
            raise web.HTTPForbidden(reason="origin_not_allowed")
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            response = await handler(request)
    except web.HTTPException as exc:
        response = web.json_response({"message": exc.reason}, status=exc.status)
    except (ValueError, TypeError, KeyError) as exc:
        code = str(exc)
        response = web.json_response(
            {"message": code},
            status=409 if code in ("idempotency_conflict", "operation_capacity") else 400,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception("request %s failed", request_id)
        response = web.json_response({"error": "internal_error"}, status=500)
    if response.status >= 400 and isinstance(response, web.Response):
        import json

        try:
            original = json.loads(response.text or "{}")
        except (ValueError, TypeError):
            original = {}
        code = original.get("reason") or original.get("error") or original.get("message")
        if not isinstance(code, str):
            code = "request_failed"
        message = original.get("message", code)
        code = {
            400: "invalid_arguments",
            401: "auth_required",
            403: "forbidden",
            404: "not_found",
            409: "invalid_state",
            429: "rate_limited",
            502: "subsystem_unavailable",
            503: "subsystem_unavailable",
            504: "outcome_unknown",
            500: "internal_error",
        }.get(response.status, code)
        if response.status == 409 and message == "localization_required":
            code = "localization_required"
        payload = {"error": {"code": code, "message": message, "request_id": request_id}}
        if "state" in original:
            payload["state"] = original["state"]
        response = web.json_response(payload, status=response.status)
        if original.get("retry_after_s") is not None:
            response.headers["Retry-After"] = str(int(original["retry_after_s"]) + 1)
    if not response.prepared:
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Request-ID"] = request_id
    if origin and permitted:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Idempotency-Key"
        response.headers["Vary"] = "Origin"
    return response


async def prepare(request, response):
    """Attach policy headers before streaming media or a WebSocket handshake starts."""
    response.headers.setdefault("X-Request-ID", request.get("request_id", ""))
    response.headers.setdefault("Cache-Control", "no-store")
    origin = request.headers.get("Origin")
    if origin and (
        origin == f"{request.scheme}://{request.host}"
        or origin in request.app["cfg"].allowed_origins
    ):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
