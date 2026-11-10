"""URL-prefix boundary, request limits and uncached authenticated responses."""

from __future__ import annotations

from . import settings


class DeploymentBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        prefix = settings.BASE_PATH
        if prefix and path not in ("/api/health", "/api/ready"):
            if not path.startswith(prefix + "/"):
                return await self.reject(send, 404)
            scope = dict(
                scope,
                path=path[len(prefix) :],
                raw_path=path[len(prefix) :].encode(),
                # Routing paths are already stripped. Adding the prefix again to
                # root_path makes Starlette's StaticFiles mount strip twice.
                root_path=scope.get("root_path", ""),
            )
        headers = dict(scope.get("headers", []))
        try:
            if int(headers.get(b"content-length", b"0")) > 1024 * 1024:
                return await self.reject(send, 413)
        except ValueError:
            return await self.reject(send, 400)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
            await send(message)

        await self.app(scope, receive, secure_send)

    @staticmethod
    async def reject(send, status):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"cache-control", b"no-store")],
            }
        )
        await send({"type": "http.response.body", "body": b""})
