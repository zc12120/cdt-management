"""Authentication and bootstrap routes with shared request guards."""

from __future__ import annotations
import ipaddress
import logging
import threading
import time
from typing import Any
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from .. import repo, security, settings, messages

log = logging.getLogger("cdt-management.auth")
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()
LOGIN_WINDOW = 300
LOGIN_MAX_FAILURES = 8


def _is_initialized() -> bool:
    return repo.get_setting("initialized") == "1"


def _session_epoch() -> int:
    try:
        return int(repo.get_setting("session_epoch") or 0)
    except Exception:
        return 0


def _current_user(request: Request) -> str | None:
    token = request.cookies.get(settings.SESSION_COOKIE, "")
    user = security.read_session(token, _session_epoch())
    if not user:
        return None
    if user != repo.get_setting("admin_username"):
        return None
    # 退出登录过的那张票据即使被翻出来也不认：会话本身无状态，光删 cookie
    # 吊销不了，票据在 14 天有效期内仍然能用。
    try:
        if repo.is_session_revoked(security.session_id(token)):
            return None
    except Exception:  # noqa: BLE001
        log.warning("会话状态暂时不可用")
        raise HTTPException(
            status_code=503, detail="会话状态暂时不可用，请稍后重试"
        ) from None
    return user


def require_admin(request: Request) -> str:
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def require_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    cookie = request.cookies.get(settings.CSRF_COOKIE, "")
    header = request.headers.get("x-cdt-csrf", "")
    if not cookie or cookie != header:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


def _attach_session(
    response: JSONResponse, username: str, epoch: int | None = None
) -> None:
    token = security.issue_session(
        username, _session_epoch() if epoch is None else epoch
    )
    response.set_cookie(
        settings.SESSION_COOKIE,
        token,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.SECURE_COOKIES,
        samesite="lax",
        path=settings.COOKIE_PATH,
    )
    response.set_cookie(
        settings.CSRF_COOKIE,
        security.new_csrf_token(),
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=False,
        secure=settings.SECURE_COOKIES,
        samesite="lax",
        path=settings.COOKIE_PATH,
    )


def _is_private_peer(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def _client_ip(request: Request) -> str:
    """Only explicitly trusted proxy CIDRs may supply the forwarded address.

    The proxy must overwrite X-Forwarded-For, not append user-provided values.
    Private-address peers are not implicitly trusted.
    """
    peer = (request.client.host if request.client else "") or "unknown"
    try:
        trusted = any(
            ipaddress.ip_address(peer) in ipaddress.ip_network(cidr)
            for cidr in settings.TRUSTED_PROXY_CIDRS
        )
    except ValueError:
        trusted = False
    if trusted:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        try:
            ipaddress.ip_address(forwarded)
        except ValueError:
            pass
        else:
            return forwarded
    return peer


def _rate_limited(client: str) -> bool:
    now = time.time()
    with _login_lock:
        # 顺手清掉过期条目：只清理当前 key 的话，字典会跟着来访 IP 一直长
        if len(_login_failures) > 256:
            for key in [
                k
                for k, v in _login_failures.items()
                if not v or now - v[-1] >= LOGIN_WINDOW
            ]:
                _login_failures.pop(key, None)
        attempts = [
            t for t in _login_failures.get(client, []) if now - t < LOGIN_WINDOW
        ]
        if attempts:
            _login_failures[client] = attempts
        else:
            _login_failures.pop(client, None)
        return len(attempts) >= LOGIN_MAX_FAILURES


def _record_failure(client: str) -> None:
    with _login_lock:
        _login_failures.setdefault(client, []).append(time.time())


def _reset_failures(client: str) -> None:
    with _login_lock:
        _login_failures.pop(client, None)


def register(app: FastAPI) -> None:
    @app.get("/api/health")
    def health():  # noqa: ANN202
        return {"status": "ok", "version": settings.VERSION}

    @app.get("/api/ready")
    def ready():
        from .. import scheduler

        result = scheduler.readiness()
        return JSONResponse(
            result, status_code=200 if result["status"] == "ready" else 503
        )

    @app.get("/api/v1/system/init-status")
    def init_status():  # noqa: ANN202
        return {
            "initialized": _is_initialized(),
            "brand": settings.BRAND,
            "version": settings.VERSION,
            "repo": settings.REPO,
        }

    @app.post("/api/v1/setup")
    def setup(payload: dict[str, Any]):  # noqa: ANN202
        if _is_initialized():
            raise HTTPException(status_code=409, detail="系统已初始化")
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if len(username) < 3:
            raise ValueError("用户名至少 3 个字符")
        if len(password) < 10:
            raise ValueError("密码至少 10 个字符")
        if len(username) > 64 or len(password) > 1024:
            raise ValueError("用户名或密码过长")
        if not repo.initialize_admin(username, password):
            raise HTTPException(status_code=409, detail="系统已初始化")
        repo.add_event(
            "system", messages.ev("系统初始化完成"), f"已初始化 / 管理员 {username}"
        )
        response = JSONResponse({"ok": True})
        _attach_session(response, username)
        return response

    @app.post("/api/v1/auth/login")
    def login(request: Request, payload: dict[str, Any]):  # noqa: ANN202
        client = _client_ip(request)
        if _rate_limited(client):
            raise HTTPException(status_code=429, detail="尝试过于频繁，请 5 分钟后再试")

        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if len(username) > 64 or len(password) > 1024:
            raise HTTPException(status_code=400, detail="用户名或密码过长")
        account_bucket = "account:" + username
        if _rate_limited(account_bucket):
            raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
        stored_user = repo.get_setting("admin_username")
        stored_hash = repo.get_setting("admin_password")

        if username != stored_user or not security.verify_password(
            password, stored_hash
        ):
            _record_failure(client)
            _record_failure(account_bucket)
            raise HTTPException(status_code=401, detail="用户名或密码不正确")

        _reset_failures(client)
        _reset_failures(account_bucket)
        response = JSONResponse({"ok": True, "username": username})
        _attach_session(response, username)
        return response

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        # 把这张票据拉黑到它自己过期为止：只删 cookie 的话，票据在有效期内
        # 依然能用（复制出来的副本照样放行）。只吊销当前这张，别把手机/平板
        # 上的登录一起踢掉 —— 那是改密码才该发生的事。
        token = request.cookies.get(settings.SESSION_COOKIE, "")
        repo.revoke_session(
            security.session_id(token), int(time.time()) + settings.SESSION_TTL_SECONDS
        )
        response = JSONResponse({"ok": True})
        response.delete_cookie(settings.SESSION_COOKIE, path=settings.COOKIE_PATH)
        response.delete_cookie(settings.CSRF_COOKIE, path=settings.COOKIE_PATH)
        return response

    @app.get("/api/v1/auth/me")
    def me(user: str = Depends(require_admin)):  # noqa: ANN202
        return {"username": user}
