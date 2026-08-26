"""Request and response models for the public API.

Every field carries a description and the models carry examples, because
`/docs` is the first thing an integrator reads. Routes previously returned bare
dicts, which documented nothing.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "session_id": "3f2a9c14-6f1e-4a63-9b1b-2f0f1f2c9d77",
                "message": "2 parathas and 3 cup chai",
            }
        }
    )

    session_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "Conversation id. Scoped to your API key, so ids only ever collide "
            "with your own. Use POST /v1/sessions to have one issued."
        ),
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="The customer's message for this turn.",
    )


class ChatResponse(BaseModel):
    reply: str = Field(..., description="Assistant's reply text for this turn.")
    state: dict[str, Any] = Field(
        ...,
        description=(
            "Full order state after the turn: location, locked restaurant, "
            "cart, totals, showcase cards and message history."
        ),
    )
    busy: bool = Field(
        False,
        description=(
            "True when every model provider was rate limited and the reply is "
            "the fallback 'system busy' notice rather than a real answer."
        ),
    )


class SessionCreateResponse(BaseModel):
    session_id: str = Field(
        ..., description="Server-issued UUID4. Unguessable, and scoped to your key."
    )
    created_at: str = Field(..., description="UTC ISO-8601 creation timestamp.")


class LocationRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"lat": 24.918, "lng": 67.091}}
    )

    lat: float = Field(..., ge=-90, le=90, description="Latitude in decimal degrees.")
    lng: float = Field(..., ge=-180, le=180, description="Longitude in decimal degrees.")


class LocationResponse(BaseModel):
    session_id: str = Field(..., description="Session the location was applied to.")
    location: str | None = Field(
        None,
        description=(
            "Karachi area name the coordinates were snapped to, or null when "
            "they fall outside the snapshot's coverage."
        ),
    )
    reason: str | None = Field(
        None,
        description="Set to 'outside_snapshot_coverage' when location is null.",
    )
    lat: float | None = Field(None, description="Latitude echoed back.")
    lng: float | None = Field(None, description="Longitude echoed back.")
    distance_km: float | None = Field(
        None, description="Distance from the coordinates to the matched area centre."
    )
    state: dict[str, Any] = Field(..., description="Order state after the update.")


class RestaurantRef(BaseModel):
    id: int | None = Field(None, description="Restaurant id, or null if none locked.")
    name: str | None = Field(None, description="Restaurant name, or null.")


class CartResponse(BaseModel):
    session_id: str = Field(..., description="Session this cart belongs to.")
    restaurant: RestaurantRef = Field(
        ..., description="Restaurant the cart is locked to."
    )
    cart: list[dict[str, Any]] = Field(
        ..., description="Lines: item_id, name, price, qty, image_url."
    )
    item_count: int = Field(..., description="Total units across all lines.")
    subtotal: float = Field(..., description="Sum of line prices before fees.")
    delivery_fee: float = Field(..., description="Estimated delivery fee.")
    platform_fee: float = Field(..., description="Estimated platform fee.")
    total: float = Field(..., description="Subtotal plus fees.")
    currency: str = Field("PKR", description="ISO currency code. Always PKR.")
    below_minimum_order: bool = Field(
        ..., description="True when subtotal is under the minimum order value."
    )
    minimum_order: float = Field(..., description="Minimum order value applied.")
    note: str | None = Field(None, description="Human-readable note about the fees.")


class HealthResponse(BaseModel):
    ok: bool = Field(..., description="True when both the model and snapshot work.")
    model: str = Field(..., description="Active model, or why none is configured.")
    database: dict[str, Any] = Field(
        ..., description="Snapshot row counts, or an error message."
    )
    database_path: str = Field(..., description="Resolved path of the snapshot.")


class TranscriptionResponse(BaseModel):
    text: str = Field(..., description="Transcript of the uploaded audio.")
    latency_ms: int = Field(..., description="Server-side transcription time.")


class SpeakRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"text": "Your order is on the way."}}
    )

    text: str = Field(
        ..., min_length=1, max_length=2000, description="Text to synthesise."
    )


class UsageDay(BaseModel):
    day: str = Field(..., description="UTC date, YYYY-MM-DD.")
    requests: int = Field(..., description="Requests admitted against the quota.")
    prompt_tokens: int = Field(..., description="Prompt tokens used that day.")
    completion_tokens: int = Field(..., description="Completion tokens used that day.")
    total_tokens: int = Field(..., description="Total tokens used that day.")


class UsageResponse(BaseModel):
    tenant: str = Field(..., description="Tenant name for the calling key.")
    tier: str = Field(..., description="Tier name for the calling key.")
    requests_per_minute: int = Field(..., description="Burst limit for this tier.")
    requests_per_day: int = Field(..., description="Daily quota for this tier.")
    days: list[UsageDay] = Field(..., description="Per-day usage, newest first.")


class ErrorResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"detail": "Missing or invalid API key."}}
    )

    detail: str = Field(..., description="Human-readable explanation.")
