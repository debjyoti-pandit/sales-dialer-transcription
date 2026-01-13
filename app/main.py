from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
import uuid
from urllib.parse import urlparse, urlunparse
from json import JSONDecodeError
from typing import Any, Optional

import websockets
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.amd import AmdEngine


app = FastAPI(title="sales-dialer-transcription-service")
logger = logging.getLogger("sales_dialer.twilio")
dg_logger = logging.getLogger("sales_dialer.deepgram")
if not logging.getLogger().handlers:
    level = (settings.log_level or "INFO").upper()
    try:
        from rich.logging import RichHandler  # type: ignore[import-not-found]  # pylint: disable=import-error

        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[
                RichHandler(
                    rich_tracebacks=True,
                    markup=True,
                    show_time=True,
                    show_level=True,
                    show_path=False,
                )
            ],
        )
    except ImportError:
        # Fallback (e.g. if rich isn't installed in some deployment).
        logging.basicConfig(level=level)

# --- In-memory live transcript state (keyed by Twilio CallSid) ---
# This lets other services/UIs fetch "what is being spoken right now" per call.
_SESSION_INFO: dict[str, dict[str, str]] = {}
_CALL_LIVE: dict[str, dict[str, Any]] = {}
_STATE_LOCK = asyncio.Lock()

# Server->server forwarding (single shared connection).
_FORWARD_QUEUE: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue(maxsize=2000)

_AMD = AmdEngine(log_path=settings.amd_log_path)


async def _forward_loop():
    """
    Maintain a single outbound WS connection to the UI backend and forward transcript events.
    This avoids opening one WS per call (which can hit tunnel/proxy limits when calls overlap).
    """
    url = (settings.transcript_ingest_ws_url or "").strip()
    if not url:
        return

    backoff_s = 0.5
    while True:
        try:
            async with websockets.connect(url, max_size=2**22) as ws:
                dg_logger.info("Forward WS connected url=%s", url)
                backoff_s = 0.5
                while True:
                    item = await _FORWARD_QUEUE.get()
                    await ws.send(json.dumps(item))
        except Exception as e:  # pylint: disable=broad-exception-caught
            dg_logger.warning("Forward WS disconnected; retrying in %.1fs err=%s", backoff_s, e)
            await asyncio.sleep(backoff_s)
            backoff_s = min(10.0, backoff_s * 2)


@app.on_event("startup")
async def _startup_background_tasks():
    if (settings.transcript_ingest_ws_url or "").strip():
        asyncio.create_task(_forward_loop())


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/calls/{call_sid}/live")
async def get_call_live(call_sid: str):
    """
    Returns the latest live transcript for a call.
    Designed for polling from a UI/backend when you have multiple concurrent calls.
    """
    async with _STATE_LOCK:
        live = _CALL_LIVE.get(call_sid)
        if not live:
            return Response(
                content=json.dumps({"call_sid": call_sid, "transcript": ""}),
                media_type="application/json",
                status_code=404,
            )
        return live


@app.get("/calls/{call_sid}/amd")
async def get_call_amd(call_sid: str):
    """
    Returns current AMD state for a call (useful for debugging / call control).
    After ~3 seconds with no decision, you'll see decision=UNKNOWN until evidence appears
    or we hard-stop at 10 seconds.
    """
    st = await _AMD.get_state(call_sid)
    if not st:
        return Response(
            content=json.dumps({"call_sid": call_sid, "amd": None}),
            media_type="application/json",
            status_code=404,
        )
    return {"call_sid": call_sid, "amd": st}


@app.post("/twilio/twiml")
async def twilio_twiml():
    """
    Convenience endpoint: returns TwiML that starts a Twilio Media Stream to /twilio/media.
    Set PUBLIC_WSS_URL to something like: wss://xxxx.ngrok-free.app/twilio/media
    """
    if not settings.public_wss_url:
        return PlainTextResponse(
            "Missing PUBLIC_WSS_URL env var (e.g. wss://<ngrok>/twilio/media)",
            status_code=500,
        )

    # Be forgiving: allow PUBLIC_WSS_URL to be provided as a bare origin and/or https URL.
    parsed = urlparse(settings.public_wss_url)
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https"):
        scheme = "wss"
    elif scheme not in ("ws", "wss"):
        scheme = "wss"
    path = parsed.path or ""
    if path in ("", "/"):
        path = "/twilio/media"
    elif not path.endswith("/twilio/media") and path != "/twilio/media":
        # If user provided some other path, keep it (but warn via logs so it's obvious).
        logger.warning(
            "PUBLIC_WSS_URL path is %s (expected /twilio/media)", parsed.path
        )
    public_stream_url = urlunparse(parsed._replace(scheme=scheme, path=path))

    # Minimal TwiML: connect a Media Stream, keep call alive.
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{public_stream_url}" />
  </Connect>
  <Pause length="600" />
</Response>
"""
    return Response(content=twiml.encode("utf-8"), media_type="application/xml")


def _dg_listen_url() -> str:
    # Twilio Media Streams sends 8kHz, mono, mu-law payloads by default.
    params = {
        "encoding": "mulaw",
        "sample_rate": "8000",
        "channels": "1",
        "interim_results": "true",
        # Encourage word-level payloads (words array + more granular interim updates)
        "words": "true",
        "punctuate": "true",
        "smart_format": "true",
        "endpointing": "50",
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"wss://api.deepgram.com/v1/listen?{qs}"


async def _deepgram_receiver(
    ws: websockets.WebSocketClientProtocol,
    session_id: str,
):
    """Receives Deepgram transcription messages and logs transcripts."""
    last_interim: str = ""
    async for msg in ws:
        try:
            data = json.loads(msg)
        except JSONDecodeError:
            continue

        # Deepgram can send non-transcript JSON (metadata/errors). If we don't log these,
        # it can look like "no response" even though Deepgram is replying.
        msg_type = (data.get("type") or "").lower()
        if msg_type == "error":
            dg_logger.error("[%s] Deepgram error: %s", session_id, data)
            continue
        if msg_type in ("metadata", "speechstarted", "utteranceend"):
            dg_logger.info("[%s] Deepgram %s: %s", session_id, msg_type, data)

        channel = data.get("channel") or {}
        alts = channel.get("alternatives") or []
        transcript = ""
        words: list[dict[str, Any]] = []
        if alts and isinstance(alts, list):
            alt0 = alts[0] or {}
            transcript = alt0.get("transcript") or ""
            w = alt0.get("words") or []
            if isinstance(w, list):
                words = [x for x in w if isinstance(x, dict)]

        if transcript:
            is_final = bool(data.get("is_final"))
            if is_final:
                # Log word-level output when Deepgram provides it.
                word_str = " ".join(
                    (w.get("word") or "").strip() for w in words
                ).strip()
                if word_str:
                    dg_logger.info("[%s] FINAL: %s", session_id, transcript)
                    dg_logger.info("[%s] WORDS: %s", session_id, word_str)
                else:
                    dg_logger.info("[%s] FINAL: %s", session_id, transcript)
            else:
                # Emit "live" transcription while the user is speaking.
                # For "instant" UI updates, do NOT throttle; only dedupe identical repeats.
                if transcript != last_interim:
                    last_interim = transcript
                    dg_logger.info("[%s] LIVE: %s", session_id, transcript)

            # Update shared state keyed by CallSid (when available).
            call_sid = ""
            agent_name = ""
            phone = ""
            call_start_monotonic: Optional[float] = None
            async with _STATE_LOCK:
                si = _SESSION_INFO.get(session_id) or {}
                call_sid = (si.get("call_sid") or "").strip()
                agent_name = (si.get("agent_name") or "").strip()
                phone = (si.get("phone") or "").strip()
                csm = si.get("call_start_monotonic")
                if isinstance(csm, (float, int)):
                    call_start_monotonic = float(csm)

            payload: Optional[dict[str, Any]] = None
            amd_result: Optional[dict[str, Any]] = None
            if call_sid:
                # Rule-based AMD expects (call_id, text, timestamp_ms, duration_ms) per chunk.
                now_m = time.monotonic()
                if call_start_monotonic is None:
                    # Fallback: if we didn't get a Twilio "start" (rare), use receiver time base.
                    call_start_monotonic = now_m
                timestamp_ms = int((now_m - call_start_monotonic) * 1000)
                duration_ms = 0
                if words:
                    try:
                        starts = [float(w.get("start")) for w in words if w.get("start") is not None]
                        ends = [float(w.get("end")) for w in words if w.get("end") is not None]
                        if starts and ends:
                            duration_ms = max(0, int((max(ends) - min(starts)) * 1000))
                    except (TypeError, ValueError):
                        duration_ms = 0

                amd_result = await _AMD.process_transcript(
                    call_id=call_sid,
                    text=transcript,
                    timestamp_ms=timestamp_ms,
                    duration_ms=duration_ms,
                    is_final=is_final,
                    word_items=words,
                )

                payload = {
                    "call_sid": call_sid,
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "phone": phone,
                    "transcript": transcript,
                    "is_final": is_final,
                    "updated_at": time.time(),
                }
                if amd_result:
                    payload["amd"] = amd_result

                async with _STATE_LOCK:
                    _CALL_LIVE[call_sid] = payload

            # Push to UI backend over websocket (best-effort)
            if payload:
                if (settings.transcript_ingest_ws_url or "").strip():
                    try:
                        _FORWARD_QUEUE.put_nowait(payload)
                    except asyncio.QueueFull:
                        # Drop if congested; next interim will supersede.
                        pass


async def _deepgram_sender(
    ws: websockets.WebSocketClientProtocol,
    audio_queue: "asyncio.Queue[Optional[bytes]]",
):
    """Pulls audio bytes from queue and sends to Deepgram; None signals shutdown."""
    while True:
        chunk = await audio_queue.get()
        if chunk is None:
            break
        if chunk:
            await ws.send(chunk)

    # Let Deepgram finalize
    try:
        await ws.send(json.dumps({"type": "CloseStream"}))
    except websockets.WebSocketException:
        pass


@app.websocket("/twilio/media")
async def twilio_media(websocket: WebSocket):
    """
    Twilio Media Streams websocket.

    Twilio sends JSON frames like:
      - {"event":"start", ...}
      - {"event":"media","media":{"payload":"<base64 mulaw bytes>"}, ...}
      - {"event":"stop", ...}
    """
    session_id = uuid.uuid4().hex[:10]
    await websocket.accept()
    connected_at = time.monotonic()
    logger.info(
        "[%s] Twilio WS connected client=%s url=%s ua=%s",
        session_id,
        getattr(websocket, "client", None),
        str(getattr(websocket, "url", "")),
        websocket.headers.get("user-agent", ""),
    )

    if not settings.deepgram_api_key:
        await websocket.close(code=1011)
        logger.error("[%s] Missing DEEPGRAM_API_KEY; closing", session_id)
        return

    audio_q: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=200)

    dg_ws: Optional[websockets.WebSocketClientProtocol] = None
    receiver_task: Optional[asyncio.Task[None]] = None
    sender_task: Optional[asyncio.Task[None]] = None
    stream_sid: str = ""
    call_sid: str = ""
    media_packets = 0
    media_bytes = 0
    dropped_packets = 0
    empty_payloads = 0
    first_media_logged = False
    last_stats_log = connected_at

    try:
        # websockets>=15 uses "additional_headers"; older versions used "extra_headers".
        try:
            dg_ws = await websockets.connect(
                _dg_listen_url(),
                additional_headers={
                    "Authorization": f"Token {settings.deepgram_api_key}"
                },
                max_size=2**22,
            )
        except TypeError:
            dg_ws = await websockets.connect(
                _dg_listen_url(),
                extra_headers={"Authorization": f"Token {settings.deepgram_api_key}"},
                max_size=2**22,
            )
        dg_logger.info("[%s] Deepgram WS connected url=%s", session_id, _dg_listen_url())
        receiver_task = asyncio.create_task(_deepgram_receiver(dg_ws, session_id))
        sender_task = asyncio.create_task(_deepgram_sender(dg_ws, audio_q))

        while True:
            frame = await websocket.receive()
            if frame.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect
            raw = frame.get("text")
            if raw is None:
                b = frame.get("bytes") or b""
                if b:
                    logger.warning(
                        "[%s] Twilio sent binary frame len=%s (expected text JSON)",
                        session_id,
                        len(b),
                    )
                continue
            try:
                msg: dict[str, Any] = json.loads(raw)
            except JSONDecodeError:
                logger.warning(
                    "[%s] Received non-JSON text frame len=%s", session_id, len(raw)
                )
                continue

            event = msg.get("event")
            if event == "start":
                start = msg.get("start") or {}
                stream_sid = start.get("streamSid") or msg.get("streamSid") or ""
                call_sid = start.get("callSid") or ""
                custom = start.get("customParameters") or {}
                agent_name = (custom.get("agent_name") or "").strip() if isinstance(custom, dict) else ""
                phone = (custom.get("phone") or "").strip() if isinstance(custom, dict) else ""
                call_start_monotonic = time.monotonic()
                logger.info(
                    "[%s] start streamSid=%s callSid=%s",
                    session_id,
                    stream_sid,
                    call_sid,
                )
                async with _STATE_LOCK:
                    _SESSION_INFO[session_id] = {
                        "call_sid": call_sid,
                        "stream_sid": stream_sid,
                        "agent_name": agent_name,
                        "phone": phone,
                        "call_start_monotonic": call_start_monotonic,
                    }
                if call_sid:
                    await _AMD.start_call(call_sid)
                continue

            if event == "media":
                media = msg.get("media") or {}
                payload_b64 = media.get("payload")
                if not payload_b64:
                    empty_payloads += 1
                    if empty_payloads == 1 or empty_payloads % 100 == 0:
                        logger.warning(
                            "[%s] media event with empty payload (count=%s) streamSid=%s",
                            session_id,
                            empty_payloads,
                            stream_sid,
                        )
                    continue
                try:
                    chunk = base64.b64decode(payload_b64)
                except (binascii.Error, ValueError):
                    logger.warning(
                        "[%s] media payload base64 decode failed b64_len=%s streamSid=%s",
                        session_id,
                        len(payload_b64),
                        stream_sid,
                    )
                    continue

                # Backpressure: if queue is full, drop frames.
                if audio_q.full():
                    dropped_packets += 1
                    if dropped_packets == 1 or dropped_packets % 100 == 0:
                        logger.warning(
                            "[%s] dropping media packets due to backpressure dropped=%s qsize=%s streamSid=%s",
                            session_id,
                            dropped_packets,
                            audio_q.qsize(),
                            stream_sid,
                        )
                    continue
                media_packets += 1
                media_bytes += len(chunk)

                if not first_media_logged:
                    first_media_logged = True
                    elapsed_ms = int((time.monotonic() - connected_at) * 1000)
                    logger.debug(
                        "[%s] FIRST media packet after %sms b64_len=%s decoded_bytes=%s track=%s streamSid=%s",
                        session_id,
                        elapsed_ms,
                        len(payload_b64),
                        len(chunk),
                        media.get("track", ""),
                        stream_sid,
                    )

                now = time.monotonic()
                if (now - last_stats_log) >= 5.0 or (media_packets % 250) == 0:
                    last_stats_log = now
                    elapsed_s = max(0.001, now - connected_at)
                    logger.debug(
                        "[%s] media stats packets=%s bytes=%s dropped=%s qsize=%s rate=%.1fpps streamSid=%s",
                        session_id,
                        media_packets,
                        media_bytes,
                        dropped_packets,
                        audio_q.qsize(),
                        media_packets / elapsed_s,
                        stream_sid,
                    )

                await audio_q.put(chunk)
                continue

            if event == "stop":
                logger.info(
                    "[%s] stop streamSid=%s callSid=%s totals packets=%s bytes=%s dropped=%s empty=%s",
                    session_id,
                    stream_sid,
                    call_sid,
                    media_packets,
                    media_bytes,
                    dropped_packets,
                    empty_payloads,
                )
                break
            if event:
                logger.info("[%s] event=%s streamSid=%s", session_id, event, stream_sid)
            else:
                logger.warning(
                    "[%s] received frame without 'event' keys=%s",
                    session_id,
                    sorted(msg.keys()),
                )

    except WebSocketDisconnect:
        logger.info(
            "[%s] Twilio WS disconnected streamSid=%s callSid=%s totals packets=%s bytes=%s dropped=%s empty=%s",
            session_id,
            stream_sid,
            call_sid,
            media_packets,
            media_bytes,
            dropped_packets,
            empty_payloads,
        )
    except (websockets.WebSocketException, OSError, RuntimeError) as e:
        logger.exception(
            "[%s] ERROR streamSid=%s callSid=%s totals packets=%s bytes=%s dropped=%s empty=%s err=%s",
            session_id,
            stream_sid,
            call_sid,
            media_packets,
            media_bytes,
            dropped_packets,
            empty_payloads,
            e,
        )
    finally:
        # Graceful shutdown so Deepgram can emit final transcripts/errors.
        try:
            await audio_q.put(None)
        except RuntimeError:
            pass

        if sender_task:
            try:
                await asyncio.wait_for(sender_task, timeout=3.0)
            except asyncio.TimeoutError:
                sender_task.cancel()
            except Exception:  # pylint: disable=broad-exception-caught
                sender_task.cancel()

        if receiver_task:
            try:
                await asyncio.wait_for(receiver_task, timeout=5.0)
            except asyncio.TimeoutError:
                receiver_task.cancel()
            except Exception:  # pylint: disable=broad-exception-caught
                receiver_task.cancel()

        if dg_ws:
            try:
                await dg_ws.close()
            except websockets.WebSocketException:
                pass

        try:
            await websocket.close()
        except RuntimeError:
            pass

        if call_sid:
            try:
                await _AMD.end_call(call_sid)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("[%s] AMD end_call failed callSid=%s", session_id, call_sid)

        logger.info("[%s] session closed", session_id)
