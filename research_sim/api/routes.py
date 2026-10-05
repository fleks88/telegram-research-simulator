from __future__ import annotations

import secrets
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .schemas import (
    CampaignSettingsPayload,
    DialogueProposalRequest,
    DialogueProposalResponse,
    MessageHistoryItem,
    PersonaProfilePayload,
    ReplyPreviewRequest,
    ReplyPreviewResponse,
    SenderAccountCreate,
    SenderAccountEnabledUpdate,
    SenderAccountResponse,
    SendMessageRequest,
    SendMessageResponse,
)
from ..services.accounts import AccountAlreadyExists
from ..services.messaging import (
    MessagingService,
    RecipientNotAllowed,
    SendRateLimitExceeded,
    SenderAccountNotAvailable,
)
from ..services.persona_research import PersonaResearchService


bearer_scheme = HTTPBearer(auto_error=False)
router = APIRouter()


def require_api_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> None:
    expected = request.app.state.settings.api_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_TOKEN is not configured",
        )
    if credentials is None or not secrets.compare_digest(
        credentials.credentials, expected
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="valid bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )


protected_router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_api_token)],
)


@protected_router.get("/accounts", response_model=list[SenderAccountResponse])
def list_sender_accounts(request: Request) -> list[SenderAccountResponse]:
    accounts = request.app.state.account_service.list()
    return [SenderAccountResponse(**account.__dict__) for account in accounts]


@protected_router.post(
    "/accounts",
    response_model=SenderAccountResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_sender_account(
    payload: SenderAccountCreate,
    request: Request,
) -> SenderAccountResponse:
    try:
        account = request.app.state.account_service.add(
            payload.account_key,
            payload.label,
        )
        request.app.state.persona_research_service.randomize_profile(
            account.account_key
        )
    except AccountAlreadyExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SenderAccountResponse(**account.__dict__)


@protected_router.patch(
    "/accounts/{account_key}",
    response_model=SenderAccountResponse,
)
def update_sender_account(
    account_key: str,
    payload: SenderAccountEnabledUpdate,
    request: Request,
) -> SenderAccountResponse:
    service = request.app.state.account_service
    try:
        found = service.set_enabled(account_key, payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not found:
        raise HTTPException(status_code=404, detail="sender account not found")
    account = next(item for item in service.list() if item.account_key == account_key)
    return SenderAccountResponse(**account.__dict__)


@protected_router.get(
    "/accounts/{account_key}/history",
    response_model=list[MessageHistoryItem],
)
def get_sender_account_history(
    account_key: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    before_id: Optional[int] = Query(default=None, ge=1),
) -> list[MessageHistoryItem]:
    accounts = request.app.state.account_service.list()
    if not any(account.account_key == account_key for account in accounts):
        raise HTTPException(status_code=404, detail="sender account not found")
    history = request.app.state.database_requests.get_account_history(
        account_key,
        limit=limit,
        before_id=before_id,
    )
    return [MessageHistoryItem(**item) for item in history]


@protected_router.get("/accounts/{account_key}/timeline")
def get_sender_account_timeline(
    account_key: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    accounts = request.app.state.account_service.list()
    if not any(account.account_key == account_key for account in accounts):
        raise HTTPException(status_code=404, detail="sender account not found")
    return request.app.state.database_requests.get_account_timeline(
        account_key,
        limit=limit,
    )


@protected_router.get("/accounts/{account_key}/persona", response_model=PersonaProfilePayload)
def get_account_persona(
    account_key: str,
    request: Request,
) -> PersonaProfilePayload:
    try:
        profile = request.app.state.persona_research_service.get_profile(account_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PersonaProfilePayload(**profile)


@protected_router.put("/accounts/{account_key}/persona", response_model=PersonaProfilePayload)
def put_account_persona(
    account_key: str,
    payload: PersonaProfilePayload,
    request: Request,
) -> PersonaProfilePayload:
    service: PersonaResearchService = request.app.state.persona_research_service
    try:
        profile = service.save_profile(account_key, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PersonaProfilePayload(**profile)


@protected_router.post(
    "/accounts/{account_key}/persona/randomize",
    response_model=PersonaProfilePayload,
)
def randomize_account_persona(
    account_key: str,
    request: Request,
) -> PersonaProfilePayload:
    try:
        profile = request.app.state.persona_research_service.randomize_profile(
            account_key
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PersonaProfilePayload(**profile)


@protected_router.post("/research/dialogues/propose", response_model=DialogueProposalResponse)
async def propose_research_dialogue(
    payload: DialogueProposalRequest,
    request: Request,
) -> DialogueProposalResponse:
    service: PersonaResearchService = request.app.state.persona_research_service
    try:
        result = await service.propose_dialogue(
            account_key=payload.account_key,
            task_prompt=payload.task_prompt,
            target_replies=payload.target_replies,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Dialogue proposal failed") from exc
    return DialogueProposalResponse(**result)


@protected_router.post("/research/reply-preview", response_model=ReplyPreviewResponse)
async def preview_research_reply(
    payload: ReplyPreviewRequest,
    request: Request,
) -> ReplyPreviewResponse:
    service: PersonaResearchService = request.app.state.persona_research_service
    try:
        result = await service.preview_reply(
            account_key=payload.account_key,
            incoming_text=payload.incoming_text,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Reply preview failed") from exc
    return ReplyPreviewResponse(**result)


@protected_router.get("/accounts/{account_key}/dialogues")
def get_account_dialogues(
    account_key: str,
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
) -> list[dict]:
    try:
        return request.app.state.persona_research_service.list_proposals(
            account_key,
            limit=limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@protected_router.get("/settings/campaign")
def get_campaign_settings(request: Request) -> Optional[dict]:
    return request.app.state.campaign_service.get_settings()


@protected_router.get("/activations/status")
def get_activation_status(request: Request) -> dict:
    state = request.app.state.database_requests.get_activation_sync_state() or {}
    config = request.app.state.campaign_service.get_settings() or {}
    return {
        "endpoint_configured": bool(request.app.state.settings.pack_activation_endpoint),
        "enabled": bool(config.get("activation_enabled")),
        "rules": config.get("activation_rules", {}),
        "first_response": state.get("first_response"),
        "last_response": state.get("last_response"),
        "last_sync_at": state.get("last_sync_at"),
        "remainders": state.get("remainders", {}),
        "pending": len(state.get("pending", [])),
    }


@protected_router.get("/auto-replies/status")
def get_auto_reply_status(request: Request) -> dict:
    pending = request.app.state.database_requests.list_pending_auto_replies(limit=50)
    return {
        "pending": len(pending),
        "items": pending,
    }


@protected_router.put("/settings/campaign", response_model=CampaignSettingsPayload)
def put_campaign_settings(
    payload: CampaignSettingsPayload,
    request: Request,
) -> dict:
    try:
        return request.app.state.campaign_service.save_settings(
            payload.model_dump(mode="json")
        )
    except (RecipientNotAllowed, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@protected_router.post("/messages/send", response_model=SendMessageResponse)
async def send_message(
    payload: SendMessageRequest,
    request: Request,
) -> SendMessageResponse:
    service: MessagingService = request.app.state.messaging_service
    try:
        result = await service.send_message(
            payload.recipient,
            payload.text,
            sender_account_index=payload.account_index,
        )
    except RecipientNotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SendRateLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except SenderAccountNotAvailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Telegram delivery failed") from exc
    return SendMessageResponse(
        delivery_id=result.delivery_id,
        recipient=result.recipient,
        sender_account=result.sender_account,
        account_index=result.sender_account_index,
        text=result.text,
        photo_attached=result.photo_attached,
    )


@protected_router.post("/messages/send-with-photo", response_model=SendMessageResponse)
async def send_message_with_photo(
    request: Request,
    recipient: str = Form(..., min_length=1, max_length=64),
    text: str = Form(..., min_length=1, max_length=1024),
    account_index: int = Form(..., ge=1),
    photo: UploadFile = File(...),
) -> SendMessageResponse:
    if photo.content_type != "image/jpeg":
        raise HTTPException(status_code=415, detail="photo must be a JPEG image")
    photo_bytes = await photo.read(10 * 1024 * 1024 + 1)
    if len(photo_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="photo must be 10 MB or smaller")
    if not photo_bytes.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=415, detail="uploaded file is not a JPEG image")

    service: MessagingService = request.app.state.messaging_service
    try:
        result = await service.send_message(
            recipient,
            text,
            sender_account_index=account_index,
            photo=photo_bytes,
        )
    except RecipientNotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SendRateLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except SenderAccountNotAvailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Telegram delivery failed") from exc
    return SendMessageResponse(
        delivery_id=result.delivery_id,
        recipient=result.recipient,
        sender_account=result.sender_account,
        account_index=result.sender_account_index,
        text=result.text,
        photo_attached=result.photo_attached,
    )


router.include_router(protected_router)
