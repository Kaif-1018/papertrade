"""WebSocket price feed endpoint: /ws/prices/{symbol}."""

from datetime import datetime, timezone
import json

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import structlog

from app.api.ws_auth import WebSocketAuthError, authenticate_websocket
from app.core.database import async_session_factory
from app.core.price_registry import (
    PriceUnavailable,
    StalePrice,
    get_price,
    is_supported,
)
from app.services.watchlist_service import WatchlistService
from app.services.ws_manager import ConnectionManager, get_ws_manager

logger = structlog.stdlib.get_logger(__name__)

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/prices/{symbol}")
async def ws_prices_endpoint(
    websocket: WebSocket,
    symbol: str,
    manager: ConnectionManager = Depends(get_ws_manager),
) -> None:
    """Stream live prices for *symbol* and support dynamic subscribe/unsubscribe/ping."""
    norm_symbol = symbol.strip().upper()

    # 1. Validate initial symbol
    if not is_supported(norm_symbol):
        await websocket.accept()
        await websocket.close(code=1008, reason=f"Symbol {norm_symbol} is not supported")
        return

    # 2. Authenticate
    try:
        user_id = await authenticate_websocket(websocket)
    except WebSocketAuthError:
        return

    # 3. Connect and subscribe to initial path symbol
    await manager.connect(websocket, user_id=user_id)
    manager.subscribe(websocket, norm_symbol)

    # 4. Immediately send latest price snapshot if available
    try:
        entry = get_price(norm_symbol)
        now = datetime.now(timezone.utc)
        age_ms = int((now - entry.received_at).total_seconds() * 1000)
        await websocket.send_json({
            "type": "snapshot",
            "symbol": entry.symbol,
            "price": str(entry.price),
            "event_time": entry.event_time.isoformat(),
            "age_ms": age_ms,
        })
    except (PriceUnavailable, StalePrice):
        pass

    # 5. Client message loop
    try:
        while True:
            try:
                raw_text = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            try:
                data = json.loads(raw_text)
                if not isinstance(data, dict):
                    await websocket.send_json({
                        "type": "error",
                        "code": "INVALID_FORMAT",
                        "message": "Payload must be a JSON object",
                    })
                    continue
            except (json.JSONDecodeError, TypeError):
                await websocket.send_json({
                    "type": "error",
                    "code": "INVALID_JSON",
                    "message": "Malformed JSON payload",
                })
                continue

            action = data.get("action")

            if action == "ping":
                await websocket.send_json({"type": "pong"})

            elif action == "subscribe":
                target_sym = data.get("symbol")
                if not target_sym or not is_supported(str(target_sym)):
                    await websocket.send_json({
                        "type": "error",
                        "code": "SYMBOL_NOT_SUPPORTED",
                        "message": f"Symbol {target_sym} is not supported",
                    })
                    continue

                norm_target = str(target_sym).strip().upper()
                ok = manager.subscribe(websocket, norm_target)
                if not ok:
                    await websocket.send_json({
                        "type": "error",
                        "code": "SUBSCRIPTION_LIMIT",
                        "message": (
                            f"Maximum topic subscriptions "
                            f"({manager.max_subscriptions_per_conn}) reached"
                        ),
                    })
                    continue

                await websocket.send_json({"type": "subscribed", "symbol": norm_target})

                # Send snapshot for newly subscribed symbol if available
                try:
                    entry = get_price(norm_target)
                    now = datetime.now(timezone.utc)
                    age_ms = int((now - entry.received_at).total_seconds() * 1000)
                    await websocket.send_json({
                        "type": "snapshot",
                        "symbol": entry.symbol,
                        "price": str(entry.price),
                        "event_time": entry.event_time.isoformat(),
                        "age_ms": age_ms,
                    })
                except Exception:
                    pass

            elif action == "unsubscribe":
                target_sym = data.get("symbol")
                if not target_sym:
                    await websocket.send_json({
                        "type": "error",
                        "code": "INVALID_REQUEST",
                        "message": "Missing symbol",
                    })
                    continue

                norm_target = str(target_sym).strip().upper()
                manager.unsubscribe(websocket, norm_target)
                await websocket.send_json({"type": "unsubscribed", "symbol": norm_target})

            elif action == "subscribe_watchlist":
                session_factory: async_sessionmaker[AsyncSession] = getattr(
                    websocket.app.state, "session_factory", async_session_factory
                )
                async with session_factory() as session:
                    service = WatchlistService(session, manager=manager)
                    symbols = await service.get_watchlist_symbols(user_id)

                ok = manager.subscribe_watchlist(websocket, symbols)
                if not ok:
                    await websocket.send_json({
                        "type": "error",
                        "code": "SUBSCRIPTION_LIMIT",
                        "message": (
                            f"Maximum topic subscriptions "
                            f"({manager.max_subscriptions_per_conn}) reached"
                        ),
                    })
                    continue

                await websocket.send_json({
                    "type": "watchlist_subscribed",
                    "symbols": symbols,
                })

                # Send snapshot for newly subscribed watchlist symbols
                for sym in symbols:
                    try:
                        entry = get_price(sym)
                        now = datetime.now(timezone.utc)
                        age_ms = int((now - entry.received_at).total_seconds() * 1000)
                        await websocket.send_json({
                            "type": "snapshot",
                            "symbol": entry.symbol,
                            "price": str(entry.price),
                            "event_time": entry.event_time.isoformat(),
                            "age_ms": age_ms,
                        })
                    except Exception:
                        pass

            elif action == "unsubscribe_watchlist":
                unsubbed = manager.unsubscribe_watchlist(websocket)
                await websocket.send_json({
                    "type": "watchlist_unsubscribed",
                    "symbols": unsubbed,
                })

            else:
                await websocket.send_json({
                    "type": "error",
                    "code": "UNKNOWN_ACTION",
                    "message": f"Unknown action: {action}",
                })

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("ws_unexpected_error", extra={"error": str(exc)})
    finally:
        manager.disconnect(websocket)
