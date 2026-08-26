"""FastAPI surface for the ordering assistant.

This contract is the product. Milestone 6 wrapped it with API-key auth, per-key
rate limiting and usage metering, and moved every functional route under /v1 so
later breaking changes cannot disturb existing integrators:

    POST /v1/sessions                  -> {session_id}
    POST /v1/chat                      {session_id, message} -> {reply, state}
    POST /v1/chat/stream               SSE tokens then {reply, state}
    POST /v1/sessions/{id}/location    {lat, lng} -> {location, state}
    POST /v1/sessions/{id}/welcome     -> {reply, state}
    GET  /v1/sessions/{id}/cart        -> cart contents
    POST /v1/sessions/{id}/confirm     -> order summary object
    POST /v1/voice/transcribe          multipart audio -> {text, latency_ms}
    POST /v1/voice/speak               {text} -> audio/wav
    GET  /v1/usage                     -> this key's usage and limits

    GET  /health                       unauthenticated, for load balancers
    GET  /docs                         OpenAPI, with an Authorize button

Every /v1 route requires `Authorization: Bearer <key>` or `X-API-Key: <key>`.
Sessions are namespaced per tenant, so ids cannot collide or leak across keys.

    Run: uvicorn api.main:app --reload  (from the repo root)
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from groq import APIError
from starlette.background import BackgroundTask

load_dotenv()

from agent.llm import NoProviderConfigured, describe  # noqa: E402
from agent.state import cart_item_count, serialize_state  # noqa: E402
from agent.tools import build_order_summary  # noqa: E402
from api import sessions  # noqa: E402
from api.models import (  # noqa: E402
    CartResponse,
    ChatRequest,
    ChatResponse,
    ErrorResponse,
    HealthResponse,
    LocationRequest,
    LocationResponse,
    SessionCreateResponse,
    SpeakRequest,
    TranscriptionResponse,
    UsageResponse,
)
from auth import store as auth_store, usage as usage_mod  # noqa: E402
from auth.api_keys import Principal  # noqa: E402
from auth.dependencies import authenticate, turn_usage  # noqa: E402
from auth.tiers import load_tiers  # noqa: E402
from auth.usage import TurnUsage  # noqa: E402
from db import queries  # noqa: E402
from db.fees import estimate_fees  # noqa: E402
from db.geo import nearest_area  # noqa: E402
from voice.client import NoGroqKey  # noqa: E402
from voice.stt import transcribe as transcribe_audio  # noqa: E402
from voice.tts import speak  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "static"
MAX_AUDIO_BYTES = 8 * 1024 * 1024

logging.getLogger("voice").setLevel(logging.INFO)
logging.getLogger("api.auth").setLevel(logging.INFO)
_timing = logging.getLogger("agent.timing")
_timing.setLevel(logging.INFO)
if not any(isinstance(h, logging.StreamHandler) for h in _timing.handlers):
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    _timing.addHandler(_handler)

API_DESCRIPTION = """
Conversational ordering assistant over a scraped Foodpanda snapshot for
Karachi. Prepares order summaries; **it does not place or track real orders.**

### Authentication
Every `/v1` route needs an API key:

    Authorization: Bearer fda_live_...

`X-API-Key: fda_live_...` is accepted as an alternative. Create a key locally
with `python -m auth.manage create-key <tenant_id>`.

### Rate limits
Limits are per key and set by your tier. Every response carries
`X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`; a `429`
also carries `Retry-After`, in seconds. Check your own usage at `GET /v1/usage`.

### Sessions
A session holds one conversation and its cart. Sessions are scoped to your API
key: ids from another key are invisible, so they can never collide or leak.
`POST /v1/sessions` issues an unguessable id.
"""

app = FastAPI(
    title="Food Ordering Assistant API",
    version="1.0.0",
    description=API_DESCRIPTION,
    openapi_tags=[
        {"name": "chat", "description": "Conversational turns."},
        {"name": "sessions", "description": "Session state, cart and checkout."},
        {"name": "voice", "description": "Speech to text and text to speech."},
        {"name": "account", "description": "Usage and limits for your key."},
        {"name": "service", "description": "Unauthenticated service endpoints."},
    ],
)

# FastAPI builds most of the schema; the security scheme is ours, so /docs
# grows an Authorize button and generated clients send the header.
def _custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Your API key. `Authorization: Bearer <key>` also works.",
        },
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": "Send your API key as a bearer token.",
        },
    }
    for path, methods in schema.get("paths", {}).items():
        if not path.startswith("/v1"):
            continue
        for operation in methods.values():
            operation["security"] = [{"ApiKeyAuth": []}, {"BearerAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def _startup() -> None:
    """Fail fast on a broken tier file rather than at the first request."""
    auth_store.init_db()
    tiers = load_tiers()
    logging.getLogger("api.auth").info(
        "tenant db=%s tiers=%s", auth_store.db_path(), ", ".join(sorted(tiers))
    )


# ---------------------------------------------------------------------------
# metering middleware
# ---------------------------------------------------------------------------

AUTH_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing, unknown or revoked key."},
    403: {"model": ErrorResponse, "description": "Tenant disabled."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
}


@app.middleware("http")
async def meter_requests(request: Request, call_next):
    """Attach rate-limit headers and record one usage row per API call.

    Runs after the route (and after a rejecting dependency), so it sees the
    real status code. The principal is stashed on `request.state` during
    authentication, before rate limiting, which is what lets a 429 still be
    attributed to the tenant that caused it.

    The write happens in a background task rather than inline because
    `call_next` returns as soon as a streaming response *starts*. Recording
    there would meter `/v1/chat/stream` before a single token had been
    generated, so every streamed turn would bill zero tokens and take a
    latency reading of nearly nothing. A background task runs after the body
    is finished, which is the only point where both are known.
    """
    started = time.perf_counter()
    response = await call_next(request)
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        return response

    for header, value in (getattr(request.state, "rate_headers", None) or {}).items():
        response.headers.setdefault(header, value)

    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    status_code = response.status_code

    def record() -> None:
        usage_mod.record_event(
            key_id=principal.key_id,
            tenant_id=principal.tenant_id,
            route=path,
            method=request.method,
            status_code=status_code,
            latency_ms=(time.perf_counter() - started) * 1000,
            usage=getattr(request.state, "turn_usage", None),
        )

    previous = response.background

    async def finish() -> None:
        if previous is not None:
            await previous()
        record()

    response.background = BackgroundTask(finish)
    return response


def _groq_http_error(exc: BaseException) -> HTTPException:
    """Map Groq SDK failures to JSON HTTP errors, never an HTML 500."""
    if isinstance(exc, NoGroqKey):
        return HTTPException(status_code=503, detail=str(exc))
    body = getattr(exc, "body", None)
    message = str(exc)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            message = str(err["message"])
        elif isinstance(err, str):
            message = err
    status = getattr(exc, "status_code", None) or 502
    lower = message.lower()
    if "terms acceptance" in lower or "model_terms_required" in lower:
        return HTTPException(
            status_code=403,
            detail=(
                "Groq Orpheus TTS needs the org admin to accept the model terms at "
                "https://console.groq.com/playground?model=canopylabs%2Forpheus-v1-english"
            ),
        )
    if status in (400, 401, 403, 413, 429):
        return HTTPException(status_code=status, detail=message)
    return HTTPException(status_code=502, detail=message)


# ---------------------------------------------------------------------------
# unauthenticated service routes
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def chat_page():
    """Minimal manual-test page. The API is the real deliverable.

    In DEMO_MODE the page is served with a key injected, so the local demo
    works without the operator pasting one in. Off by default: anyone who can
    load the page can read that key, which is fine on a laptop and not fine in
    production.
    """
    page = STATIC_DIR / "chat.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="static/chat.html not found")
    demo_key = os.getenv("DEMO_API_KEY", "").strip()
    if os.getenv("DEMO_MODE", "").strip().lower() in {"1", "true", "yes"} and demo_key:
        # The placeholder is quoted in the page, so the unsubstituted file is
        # still valid JavaScript; the quotes are part of what gets replaced.
        html = page.read_text(encoding="utf-8").replace(
            '"__DEMO_API_KEY__"', json.dumps(demo_key)
        )
        return HTMLResponse(html)
    return FileResponse(page)


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["service"],
    summary="Service health",
    description=(
        "Whether the model and snapshot are usable. Unauthenticated so a load "
        "balancer can poll it. Always returns 200; read `ok`."
    ),
)
def health() -> HealthResponse:
    try:
        model = describe()
        model_ok = True
    except NoProviderConfigured as exc:
        model = str(exc)
        model_ok = False

    try:
        with queries.session() as conn:
            restaurants = conn.execute(
                "SELECT COUNT(*) AS n FROM restaurants"
            ).fetchone()["n"]
            items = conn.execute(
                "SELECT COUNT(*) AS n FROM menu_items"
            ).fetchone()["n"]
        db_ok = True
        db_detail: dict[str, Any] = {"restaurants": restaurants, "menu_items": items}
    except Exception as exc:  # noqa: BLE001 - health must report, not raise
        db_ok = False
        db_detail = {"error": str(exc)}

    return HealthResponse(
        ok=model_ok and db_ok,
        model=model,
        database=db_detail,
        database_path=str(queries.db_path()),
    )


# ---------------------------------------------------------------------------
# /v1
# ---------------------------------------------------------------------------

v1 = APIRouter(prefix="/v1")


@v1.post(
    "/sessions",
    response_model=SessionCreateResponse,
    status_code=201,
    tags=["sessions"],
    responses=AUTH_ERRORS,
    summary="Create a session",
    description=(
        "Issues an unguessable session id. Optional — you may supply your own "
        "id on any session route, since ids are scoped to your key either way."
    ),
)
def create_session(
    principal: Principal = Depends(authenticate),
) -> SessionCreateResponse:
    return SessionCreateResponse(
        session_id=str(uuid.uuid4()), created_at=auth_store.utc_now()
    )


@v1.post(
    "/chat",
    response_model=ChatResponse,
    tags=["chat"],
    responses={
        **AUTH_ERRORS,
        502: {"model": ErrorResponse, "description": "Model provider failed."},
        503: {"model": ErrorResponse, "description": "No provider or snapshot."},
    },
    summary="Send a message",
    description=(
        "Runs one conversational turn: the assistant may search restaurants, "
        "lock one, and add items to the cart before replying. Token usage is "
        "metered against your key."
    ),
)
def chat(
    request: ChatRequest,
    principal: Principal = Depends(authenticate),
    usage: TurnUsage = Depends(turn_usage),
) -> ChatResponse:
    try:
        values = sessions.send_message(
            request.session_id, request.message, principal.namespace, usage
        )
    except NoProviderConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # Groq/OpenAI errors (retired model, rate limit, etc.) otherwise land
        # as Starlette's HTML "Internal Server Error", which the chat page
        # cannot parse as JSON.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ChatResponse(
        reply=sessions.latest_reply(values),
        state=serialize_state(values),
        busy=sessions.last_turn_is_busy(values),
    )


@v1.post(
    "/chat/stream",
    tags=["chat"],
    responses=AUTH_ERRORS,
    summary="Send a message, streamed",
    description=(
        "Server-sent events. Emits `{\"token\": \"...\"}` for each piece of the "
        "reply as it is generated, then one terminal event: either "
        "`{\"done\": true, \"reply\": ..., \"state\": ..., \"busy\": ...}` or "
        "`{\"error\": \"...\"}`. Tool rounds are silent, so tokens begin when "
        "the assistant starts writing its answer. Transport errors arrive as "
        "an `error` event on a 200 response, not as an HTTP error status."
    ),
)
def chat_stream(
    request: ChatRequest,
    principal: Principal = Depends(authenticate),
    usage: TurnUsage = Depends(turn_usage),
):
    def events():
        try:
            for kind, payload in sessions.iter_chat_events(
                request.session_id, request.message, principal.namespace, usage
            ):
                if kind == "token":
                    yield f"data: {json.dumps({'token': payload})}\n\n"
                elif kind == "done":
                    body = {
                        "done": True,
                        "reply": sessions.latest_reply(payload),
                        "state": serialize_state(payload),
                        "busy": sessions.last_turn_is_busy(payload),
                    }
                    yield f"data: {json.dumps(body)}\n\n"
                elif kind == "error":
                    yield f"data: {json.dumps({'error': str(payload)})}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@v1.post(
    "/sessions/{session_id}/location",
    response_model=LocationResponse,
    tags=["sessions"],
    responses=AUTH_ERRORS,
    summary="Set the delivery area",
    description=(
        "Snaps browser coordinates to the nearest Karachi area this snapshot "
        "can search, within 25 km. Returns `location: null` with "
        "`reason: outside_snapshot_coverage` when nothing is near enough."
    ),
)
def set_session_location(
    session_id: str,
    request: LocationRequest,
    principal: Principal = Depends(authenticate),
) -> LocationResponse:
    match = nearest_area(request.lat, request.lng)
    if match is None:
        return LocationResponse(
            session_id=session_id,
            location=None,
            reason="outside_snapshot_coverage",
            state=serialize_state(
                sessions.get_values(session_id, principal.namespace)
            ),
        )
    area, distance_km = match
    values = sessions.set_location(session_id, area, principal.namespace)
    return LocationResponse(
        session_id=session_id,
        location=area,
        lat=request.lat,
        lng=request.lng,
        distance_km=round(distance_km, 2),
        state=serialize_state(values),
    )


@v1.post(
    "/sessions/{session_id}/welcome",
    response_model=ChatResponse,
    tags=["sessions"],
    responses=AUTH_ERRORS,
    summary="Opening message",
    description=(
        "The first chat bubble, branching on whether a location is already "
        "set. Costs nothing: it is a template, not a model call."
    ),
)
def session_welcome(
    session_id: str, principal: Principal = Depends(authenticate)
) -> ChatResponse:
    return ChatResponse(
        reply=sessions.welcome_reply(session_id, principal.namespace),
        state=serialize_state(sessions.get_values(session_id, principal.namespace)),
    )


@v1.get(
    "/sessions/{session_id}/cart",
    response_model=CartResponse,
    tags=["sessions"],
    responses={
        **AUTH_ERRORS,
        404: {"model": ErrorResponse, "description": "No such session for this key."},
    },
    summary="Read the cart",
    description="Current cart with fee estimates. 404 if the session has no history.",
)
def get_cart(
    session_id: str, principal: Principal = Depends(authenticate)
) -> CartResponse:
    values = sessions.get_values(session_id, principal.namespace)
    if not values:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
    cart = values.get("cart") or []
    totals = estimate_fees(cart)
    return CartResponse(
        session_id=session_id,
        restaurant={
            "id": values.get("restaurant_id"),
            "name": values.get("restaurant_name"),
        },
        cart=cart,
        item_count=cart_item_count(cart),
        subtotal=totals["subtotal"],
        delivery_fee=totals["delivery_fee"],
        platform_fee=totals["platform_fee"],
        total=totals["total"],
        currency="PKR",
        below_minimum_order=totals["below_minimum_order"],
        minimum_order=totals["minimum_order"],
        note=totals["fee_note"],
    )


@v1.post(
    "/sessions/{session_id}/confirm",
    tags=["sessions"],
    responses={
        **AUTH_ERRORS,
        404: {"model": ErrorResponse, "description": "No such session for this key."},
        409: {"model": ErrorResponse, "description": "No restaurant locked, or empty cart."},
    },
    summary="Confirm the order",
    description=(
        "Finalises the cart into an order summary. Uses the same builder as "
        "the assistant's own confirm tool, so both paths produce identical "
        "objects. **No real order is placed.**"
    ),
)
def confirm(
    session_id: str, principal: Principal = Depends(authenticate)
) -> dict[str, Any]:
    values = sessions.get_values(session_id, principal.namespace)
    if not values:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
    if values.get("restaurant_id") is None:
        raise HTTPException(
            status_code=409,
            detail="No restaurant locked for this session yet.",
        )
    if not values.get("cart"):
        raise HTTPException(status_code=409, detail="Cart is empty.")

    summary = build_order_summary(values)
    sessions.update_values(
        session_id, {"order_summary": summary}, principal.namespace
    )
    return summary


@v1.get(
    "/usage",
    response_model=UsageResponse,
    tags=["account"],
    responses=AUTH_ERRORS,
    summary="Your usage and limits",
    description=(
        "Per-day request and token counts for the calling key, newest first, "
        "alongside the limits of your tier."
    ),
)
def get_usage(principal: Principal = Depends(authenticate)) -> UsageResponse:
    return UsageResponse(
        tenant=principal.tenant_name,
        tier=principal.tier.name,
        requests_per_minute=principal.tier.requests_per_minute,
        requests_per_day=principal.tier.requests_per_day,
        days=usage_mod.usage_summary(principal.key_id),
    )


@v1.post(
    "/voice/transcribe",
    response_model=TranscriptionResponse,
    tags=["voice"],
    responses={
        **AUTH_ERRORS,
        400: {"model": ErrorResponse, "description": "Empty or unusable audio."},
        413: {"model": ErrorResponse, "description": "Audio larger than 8 MB."},
    },
    summary="Transcribe audio",
    description=(
        "Whisper speech-to-text. Send `audio` as multipart form data; wav, "
        "mp3, m4a, ogg and webm are accepted, up to 8 MB. Returns the "
        "transcript without sending it as a message."
    ),
)
async def voice_transcribe(
    audio: UploadFile = File(..., description="Recording to transcribe."),
    principal: Principal = Depends(authenticate),
) -> TranscriptionResponse:
    data = await audio.read()
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio too large (max 8 MB).")
    if len(data) < 32:
        raise HTTPException(status_code=400, detail="Audio recording is empty.")

    filename = audio.filename or "audio.webm"
    content_type = (audio.content_type or "").split(";")[0].strip() or "audio/webm"
    filename, content_type = _normalize_audio_meta(filename, content_type)
    try:
        text, latency_ms = transcribe_audio(
            data, filename=filename, content_type=content_type
        )
    except NoGroqKey as exc:
        raise _groq_http_error(exc) from exc
    except APIError as exc:
        mapped = _groq_http_error(exc)
        if mapped.status_code in (400, 415, 422) or _looks_like_format_error(exc):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Audio format not accepted. Retry with audio/wav if your "
                    "browser supports it. "
                    + str(mapped.detail)
                ),
            ) from exc
        raise mapped from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return TranscriptionResponse(text=text, latency_ms=latency_ms)


def _looks_like_format_error(exc: BaseException) -> bool:
    blob = f"{exc} {getattr(exc, 'body', '')}".lower()
    return any(token in blob for token in ("format", "unsupported", "codec", "mime"))


def _normalize_audio_meta(filename: str, content_type: str) -> tuple[str, str]:
    """Strip codec suffixes and keep Groq Whisper's expected extensions."""
    raw = (content_type or "").split(";")[0].strip().lower()
    name = filename or "audio.webm"
    if raw in {"audio/wav", "audio/x-wav", "audio/wave"}:
        return ("audio.wav" if not name.lower().endswith(".wav") else name), "audio/wav"
    if raw in {"audio/mpeg", "audio/mp3"}:
        return ("audio.mp3" if not name.lower().endswith(".mp3") else name), "audio/mpeg"
    if raw in {"audio/mp4", "audio/m4a", "audio/x-m4a"}:
        return ("audio.m4a" if not name.lower().endswith((".m4a", ".mp4")) else name), "audio/mp4"
    if raw in {"audio/ogg", "application/ogg"}:
        return ("audio.ogg" if not name.lower().endswith(".ogg") else name), "audio/ogg"
    if not name.lower().endswith(".webm"):
        name = "audio.webm"
    return name, "audio/webm"


@v1.post(
    "/voice/speak",
    tags=["voice"],
    responses={
        **AUTH_ERRORS,
        200: {
            "content": {"audio/wav": {}},
            "description": "WAV audio. `X-TTS-Latency-Ms` and `X-TTS-Chunks` headers.",
        },
        400: {"model": ErrorResponse, "description": "Text empty or too long."},
    },
    summary="Synthesise speech",
    description="Orpheus text-to-speech. Chunking and WAV concatenation happen server-side.",
)
def voice_speak(
    request: SpeakRequest, principal: Principal = Depends(authenticate)
) -> Response:
    try:
        wav, latency_ms, chunks = speak(request.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (NoGroqKey, APIError) as exc:
        raise _groq_http_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "X-TTS-Latency-Ms": str(latency_ms),
            "X-TTS-Chunks": str(chunks),
        },
    )


app.include_router(v1)
