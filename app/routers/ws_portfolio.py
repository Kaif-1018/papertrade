"""Private WebSocket portfolio valuation endpoint: /ws/portfolio/{user_id}."""

import asyncio
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import structlog

from app.api.ws_auth import WebSocketAuthError, authenticate_websocket_with_claims
from app.core.config import get_settings
from app.core.database import async_session_factory
from app.core.exceptions import NotFoundError
from app.services.portfolio_service import PortfolioService
from app.services.ws_manager import ConnectionManager, get_ws_manager

logger = structlog.stdlib.get_logger(__name__)

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/portfolio/{user_id}")
async def ws_portfolio_endpoint(
    websocket: WebSocket,
    user_id: UUID,
    manager: ConnectionManager = Depends(get_ws_manager),
) -> None:
    """Stream private real-time portfolio updates and trade confirmations to the user.

    Security:
    - User can only connect to their own user_id.
    - Admins can connect to any user_id.
    - Rejects unauthorized attempts with close code 1008.

    Push triggers:
    - Initial portfolio snapshot upon connection.
    - Targeted post-commit trade confirmations & updated snapshots via manager.send_to_user.
    - Periodic refresh (every PORTFOLIO_REFRESH_INTERVAL_SECONDS) when live prices change unrealised PnL.
    """
    settings = get_settings()

    # 1. Authenticate and extract caller claims
    try:
        caller_id, role = await authenticate_websocket_with_claims(websocket)
    except WebSocketAuthError:
        return

    # 2. Authorization check: self or admin
    if role != "ADMIN" and caller_id != user_id:
        await websocket.accept()
        await websocket.close(
            code=1008,
            reason="Forbidden: cannot connect to another user's portfolio",
        )
        return

    # 3. Register user connection
    await manager.connect(websocket, user_id=user_id)

    session_factory = getattr(
        websocket.app.state, "session_factory", async_session_factory
    )

    # 4. Deliver initial portfolio snapshot
    last_payload: dict[str, Any] = {}
    try:
        async with session_factory() as session:
            service = PortfolioService(session)
            portfolio = await service.get_portfolio(user_id)
        last_payload = portfolio.model_dump(mode="json")
        await websocket.send_json({
            "type": "portfolio_update",
            "portfolio": last_payload,
        })
    except NotFoundError:
        manager.disconnect(websocket)
        await websocket.close(code=1008, reason=f"User {user_id} not found")
        return
    except Exception as exc:
        logger.warning(
            "ws_portfolio_initial_snapshot_failed",
            user_id=str(user_id),
            error=str(exc),
        )

    # 5. Background periodic refresh task
    async def _periodic_refresher() -> None:
        nonlocal last_payload
        interval = settings.PORTFOLIO_REFRESH_INTERVAL_SECONDS
        while True:
            await asyncio.sleep(interval)
            try:
                async with session_factory() as session:
                    service = PortfolioService(session)
                    fresh = await service.get_portfolio(user_id)
                fresh_dict = fresh.model_dump(mode="json")

                # Skip push if valuation metrics haven't changed (ignore timestamp)
                fresh_cmp = {k: v for k, v in fresh_dict.items() if k != "as_of"}
                last_cmp = {k: v for k, v in last_payload.items() if k != "as_of"}

                if fresh_cmp != last_cmp:
                    await websocket.send_json({
                        "type": "portfolio_update",
                        "portfolio": fresh_dict,
                    })
                    last_payload = fresh_dict
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(
                    "ws_portfolio_refresh_error",
                    user_id=str(user_id),
                    error=str(exc),
                )

    refresh_task = asyncio.create_task(_periodic_refresher())

    # 6. Client message loop (handles pings, keeps connection alive)
    try:
        while True:
            try:
                raw_text = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            try:
                data = json.loads(raw_text)
                if isinstance(data, dict) and data.get("action") == "ping":
                    await websocket.send_json({"type": "pong"})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning(
            "ws_portfolio_unexpected_error",
            user_id=str(user_id),
            error=str(exc),
        )
    finally:
        manager.disconnect(websocket)
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
