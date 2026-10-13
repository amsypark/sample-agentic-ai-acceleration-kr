# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Refuse a ``service_tier`` the gateway cannot price, before anything is reserved or sent.

The OpenAI-dialect routes forward the client's body to Bedrock with only ``model``
rewritten, so a client-supplied ``service_tier`` reaches the upstream verbatim.
``model_pricings`` holds Standard rates only. A request served on another tier is
therefore billed by AWS at that tier's rate while ``usage_logs`` and the Redis budget
counters record (and deduct) the Standard rate — the budget ceiling stops meaning what
the console says it means.

Measured 2026-10-14, us-east-2 bedrock-runtime ``/openai/v1/responses``, SigV4::

    model                    service_tier      status  response.service_tier
    us.openai.gpt-6-astra    (absent)          200     'default'
    us.openai.gpt-6-astra    'auto'            200     'default'
    us.openai.gpt-6-astra    'default'         200     'default'
    us.openai.gpt-6-astra    'ultrafast'       200     'ultrafast'   <- billed 6x
    us.openai.gpt-6.1-sol    'ultrafast'       200     'ultrafast'   <- billed 6x
    us.openai.gpt-6-astra    'priority'|'flex' 400
    us.openai.gpt-6-sol      'ultrafast'       400
    us.openai.gpt-5.6-*      'priority'|'flex'|'ultrafast'  400

and on Mantle (us-east-2 ``/openai/v1/responses``, bearer), same day::

    openai.gpt-5.5, openai.gpt-5.6-{sol,terra,luna}  'priority'|'flex'|'ultrafast'  400

The model cards put Ultrafast at six times Standard. Before GPT-6 Astra / 6.1 Sol were
registered (migration 0041) every non-Standard tier was already a 400 upstream, so this
gate changes no outcome for the models that existed before it — it only stops the first
tier that Bedrock actually accepts from being under-recorded.

Why refuse instead of stripping the field: a silent downgrade hands the client a
Standard-speed answer it did not ask for and hides the reason. A 400 that names the
field is what the upstream itself returns for every tier it does not serve here, so
clients already handle it.

Why only the two Bedrock OpenAI planes: the Anthropic dialect never forwards the field
(routers/messages.py ``_BEDROCK_ALLOWED_FIELDS`` drops it and ``invoke_model`` is called
without a tier), and the in-house vLLM path is not billed per tier.

⚠️ To serve another tier later, price it first (a tier dimension on ``model_pricings``
and in ``cost_recorder.calculate_cost``), then add it to ``_PRICED_TIERS``. Adding it
here alone re-opens the gap this module closes.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi.responses import JSONResponse

from app.schemas.domain import ProviderType

logger = structlog.get_logger(__name__)

# Values that resolve to the Standard tier on Bedrock (measured above: both come back as
# response.service_tier='default'). Absent and JSON null are treated the same as these.
_PRICED_TIERS = frozenset({"auto", "default"})

_GATED_PROVIDERS = frozenset(
    {ProviderType.BEDROCK_RUNTIME_OPENAI, ProviderType.BEDROCK_MANTLE_OPENAI}
)


def reject_unpriced_service_tier(
    req_data: Any, provider: Any, *, alias: str = "", request_id: str = ""
) -> JSONResponse | None:
    """Return a 400 for a tier the gateway has no price for, else None.

    Must run before ``enforce_rate_limits``: a rejection after the pre-reservation would
    hold TPM and cost the request never used (tests/regression/
    test_high_reservation_release_on_error.py describes that defect class).
    """
    if provider not in _GATED_PROVIDERS or not isinstance(req_data, dict):
        return None
    tier = req_data.get("service_tier")
    # isinstance before the set lookup: an unhashable value (list/dict) would raise
    # TypeError on `in frozenset` and turn a client error into a 500.
    if tier is None or (isinstance(tier, str) and tier in _PRICED_TIERS):
        return None

    shown = tier if isinstance(tier, str) else type(tier).__name__
    logger.warning(
        "service_tier_rejected",
        request_id=request_id,
        alias=alias,
        service_tier=str(shown)[:32],
    )
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "type": "invalid_request_error",
                "param": "service_tier",
                "code": "unsupported_service_tier",
                "message": (
                    f"service_tier '{str(shown)[:32]}' is not available through this gateway: "
                    "only the Standard tier is priced and budgeted. Omit the field or send "
                    "'default'."
                ),
            }
        },
    )
