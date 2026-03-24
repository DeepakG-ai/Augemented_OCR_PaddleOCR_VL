import asyncio
import json
import logging

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

TERMINAL_STATUSES = {"completed", "failed", "needs_review"}
WS_TIMEOUT_SECONDS = 300  # 5-minute timeout


@router.websocket("/ws/jobs/{job_id}")
async def ws_job_status(websocket: WebSocket, job_id: str):
    """
    Real-time job status stream via Redis pub/sub.
    Message shapes:
      {"status": "processing", "job_id": "..."}
      {"status": "completed", "job_id": "...", "data": {...}}
      {"status": "needs_review", "job_id": "...", "data": {...}, "candidates": {...}}
      {"status": "failed", "job_id": "...", "error": "..."}
    """
    await websocket.accept()

    redis_client = aioredis.from_url(settings.REDIS_URL)
    pubsub = redis_client.pubsub()
    channel = f"job:{job_id}"

    try:
        await pubsub.subscribe(channel)
        logger.info(f"WebSocket client subscribed to {channel}")

        # Listen for messages with timeout
        while True:
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                    timeout=WS_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                # 5-minute timeout — close connection
                await websocket.send_json({
                    "status": "timeout",
                    "job_id": job_id,
                    "error": "Connection timed out after 5 minutes",
                })
                break

            if message is None:
                # No message yet, small sleep to avoid busy loop
                await asyncio.sleep(0.1)
                continue

            if message["type"] == "message":
                data = json.loads(message["data"])
                await websocket.send_json(data)

                # Close after terminal status
                if data.get("status") in TERMINAL_STATUSES:
                    logger.info(f"Terminal status for {job_id}: {data['status']}")
                    break

    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected from {channel}")
    except Exception as e:
        logger.error(f"WebSocket error for {channel}: {e}")
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()
        await redis_client.close()
        try:
            await websocket.close()
        except Exception:
            pass
