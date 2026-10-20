from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from .gateway import InferenceRequest
from .store import ACCOUNT_ALLOWANCE, SESSION_SECONDS, CloudError

SESSION_COOKIE = "__Host-guardmate-session"
FLOW_COOKIE = "__Host-guardmate-oauth"


class PrivateCloudRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_response(request):
            response = await handler(request)
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        return private_response


class Activation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential: str = Field(min_length=20, max_length=4096, repr=False)


def build_cloud_router(service=None, *, oauth=None, downloads=None, public_origin=""):
    router = APIRouter(prefix="/api/cloud", route_class=PrivateCloudRoute)
    secure = not public_origin.startswith("http://")
    session_cookie = SESSION_COOKIE if secure else "guardmate-dev-session"
    flow_cookie = FLOW_COOKIE if secure else "guardmate-dev-oauth"

    def require_service():
        if service is None:
            raise CloudError("cloud_unavailable", 503)
        return service

    def bearer(authorization):
        if not authorization or not authorization.startswith("Bearer "):
            raise CloudError("sign_in_required", 401)
        return require_service().store.authenticate(authorization[7:], kind="api")

    def browser(request):
        return require_service().store.authenticate(request.cookies.get(session_cookie), kind="web")

    def same_origin(request):
        if (
            request.headers.get("origin", public_origin) != public_origin
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            raise CloudError("cross_origin_request", 403)

    def safe(callback):
        try:
            return callback()
        except CloudError as error:
            return JSONResponse(
                {"detail": error.code},
                status_code=error.status_code,
                headers={"Cache-Control": "no-store"},
            )
        except Exception:
            return JSONResponse(
                {"detail": "cloud_unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

    async def read_body(request, model):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 256_000:
                raise CloudError("request_too_large", 413)
            body.extend(chunk)
        try:
            return model.model_validate_json(body)
        except ValidationError:
            # Validation responses must never echo credentials, prompts or resident context.
            raise CloudError("invalid_request", 422) from None

    @router.get("/config")
    def config():
        return {
            "oauth_ready": service is not None and oauth is not None,
            "download_ready": service is not None and downloads is not None,
            "signin_url": "/api/cloud/auth/google/start",
            "budget_microdollars": ACCOUNT_ALLOWANCE,
        }

    @router.get("/auth/google/start")
    def google_start():
        def run():
            require_service()
            if oauth is None:
                raise CloudError("google_sign_in_unavailable", 503)
            url, browser_token = oauth.start()
            response = RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})
            response.set_cookie(
                flow_cookie,
                browser_token,
                max_age=300,
                secure=secure,
                httponly=True,
                samesite="lax",
                path="/",
            )
            return response

        return safe(run)

    @router.get("/auth/google/callback")
    def google_callback(request: Request, code: str = "", state: str = ""):
        def run():
            require_service()
            if oauth is None:
                raise CloudError("google_sign_in_unavailable", 503)
            session = oauth.complete(code, state, request.cookies.get(flow_cookie))
            response = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store"})
            response.set_cookie(
                session_cookie,
                session["access_token"],
                max_age=SESSION_SECONDS,
                secure=secure,
                httponly=True,
                samesite="lax",
                path="/",
            )
            response.delete_cookie(
                flow_cookie, path="/", secure=secure, httponly=True, samesite="lax"
            )
            return response

        return safe(run)

    @router.get("/me")
    def me(request: Request):
        def run():
            account_id = browser(request)
            return {"authenticated": True, **service.store.account_status(account_id)}

        return safe(run)

    @router.post("/logout")
    def logout(request: Request):
        def run():
            same_origin(request)
            require_service().store.logout(request.cookies.get(session_cookie, ""))
            response = JSONResponse({"authenticated": False})
            response.delete_cookie(
                session_cookie, path="/", secure=secure, httponly=True, samesite="lax"
            )
            return response

        return safe(run)

    @router.get("/download/windows")
    def windows_download(request: Request):
        def run():
            same_origin(request)
            account_id = browser(request)
            if downloads is None:
                raise CloudError("download_unavailable", 503)
            credential = service.store.create_credential(account_id)
            try:
                content = downloads.personalized(public_origin, credential)
            except Exception:
                from .store import digest_token

                service.store.revoke_credential(account_id, digest_token(credential))
                raise CloudError("download_unavailable", 503) from None
            return Response(
                content,
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": 'attachment; filename="GuardMateSetup.exe"',
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        return safe(run)

    @router.post("/activate")
    async def activate(request: Request):
        try:
            require_service()
            body = await read_body(request, Activation)
        except CloudError as error:
            return JSONResponse(
                {"detail": error.code},
                status_code=error.status_code,
                headers={"Cache-Control": "no-store"},
            )
        return await run_in_threadpool(safe, lambda: service.store.activate(body.credential))

    @router.get("/account")
    def account(authorization: Annotated[str | None, Header()] = None):
        return safe(lambda: require_service().store.account_status(bearer(authorization)))

    @router.get("/release")
    def release(authorization: Annotated[str | None, Header()] = None):
        def run():
            bearer(authorization)
            if downloads is None:
                raise CloudError("download_unavailable", 503)
            return downloads.manifest()

        return safe(run)

    @router.post("/inference")
    async def inference(request: Request, authorization: Annotated[str | None, Header()] = None):
        try:
            account_id = await run_in_threadpool(bearer, authorization)
            body = await read_body(request, InferenceRequest)
        except CloudError as error:
            return JSONResponse(
                {"detail": error.code},
                status_code=error.status_code,
                headers={"Cache-Control": "no-store"},
            )
        return await run_in_threadpool(safe, lambda: service.infer(account_id, body))

    @router.get("/credentials")
    def credentials(request: Request):
        def run():
            account_id = browser(request)
            return {
                "credentials": [
                    {"credential_id": item["_id"], "revoked": item["revoked"]}
                    for item in service.store.credentials.find({"account_id": account_id})
                ]
            }

        return safe(run)

    @router.post("/credentials/{credential_id}/revoke")
    def revoke(credential_id: str, request: Request):
        def run():
            same_origin(request)
            account_id = browser(request)
            return {"revoked": service.store.revoke_credential(account_id, credential_id)}

        return safe(run)

    return router
