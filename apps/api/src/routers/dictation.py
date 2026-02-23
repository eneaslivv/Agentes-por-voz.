"""
Dictation WebSocket Router
===========================
Handles voice dictation with auto-type capability.
Supports both batch (single WAV) and streaming (chunked PCM) protocols.
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
import structlog
import asyncio
import struct
import time
import pyautogui
import pyperclip

from src.services.asr import transcribe_audio

router = APIRouter()
logger = structlog.get_logger()

# Configure pyautogui
pyautogui.FAILSAFE = False

# Streaming constants
STREAM_SAMPLE_RATE = 16000
STREAM_CHANNELS = 1
STREAM_BITS_PER_SAMPLE = 16


def type_text_via_clipboard(text: str):
    """
    Type text by copying to clipboard and pressing Ctrl+V.
    Releases all modifier keys first to prevent Ctrl+Shift+V issues.
    """
    try:
        for key in ['shift', 'ctrl', 'alt', 'win', 'winleft', 'winright']:
            try:
                pyautogui.keyUp(key)
            except Exception:
                pass

        time.sleep(0.15)
        pyperclip.copy(text)
        time.sleep(0.25)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.1)

        logger.info("Auto-type completed", chars=len(text))
    except Exception as e:
        logger.error("Auto-type failed", error=str(e))


def make_wav_header(data_size: int) -> bytes:
    """Create a WAV header for raw PCM int16 mono 16kHz data."""
    byte_rate = STREAM_SAMPLE_RATE * STREAM_CHANNELS * STREAM_BITS_PER_SAMPLE // 8
    block_align = STREAM_CHANNELS * STREAM_BITS_PER_SAMPLE // 8
    header = struct.pack('<4sI4s', b'RIFF', 36 + data_size, b'WAVE')
    header += struct.pack('<4sIHHIIHH', b'fmt ', 16, 1,
                          STREAM_CHANNELS, STREAM_SAMPLE_RATE,
                          byte_rate, block_align, STREAM_BITS_PER_SAMPLE)
    header += struct.pack('<4sI', b'data', data_size)
    return header


async def _process_and_respond(websocket, session_id, auto_type, audio_data):
    """Transcribe audio and send response. Shared by batch and streaming."""
    try:
        transcript = await transcribe_audio(audio_data, delayed=True)

        if transcript:
            transcript = transcript.replace("Vale", "Dale").replace("vale", "dale")

        if transcript and transcript.strip():
            logger.info(f"Transcript: '{transcript[:100]}'")

            await websocket.send_json({
                "type": "transcript",
                "text": transcript,
                "session_id": session_id,
            })

            if auto_type:
                logger.info(f"Auto-typing {len(transcript)} chars")
                try:
                    await asyncio.to_thread(type_text_via_clipboard, transcript)
                    await websocket.send_json({
                        "type": "typed",
                        "success": True,
                    })
                except Exception as e:
                    logger.error("Auto-type error", error=str(e))
                    await websocket.send_json({
                        "type": "typed",
                        "success": False,
                        "error": str(e),
                    })
        else:
            logger.warning("Empty transcript")
            await websocket.send_json({
                "type": "transcript",
                "text": "",
                "session_id": session_id,
            })

    except Exception as e:
        logger.error("Transcription error", error=str(e))
        await websocket.send_json({
            "type": "error",
            "message": str(e),
        })


async def _handle_streaming(websocket, session_id, auto_type):
    """Handle chunked PCM streaming protocol.

    Client sends raw PCM int16 chunks as binary frames.
    When done, client sends b"DONE" (4 bytes).
    Server assembles WAV and transcribes.
    """
    audio_chunks = []
    total_bytes = 0

    while True:
        data = await websocket.receive()
        if data.get("type") == "websocket.disconnect":
            return

        if "bytes" in data:
            chunk = data["bytes"]
            if chunk == b"DONE":
                logger.info(f"Stream complete: {total_bytes} bytes")
                break
            audio_chunks.append(chunk)
            total_bytes += len(chunk)

    if total_bytes < 1000:
        await websocket.send_json({
            "type": "error",
            "message": "Audio demasiado corto",
        })
        return

    # Assemble PCM chunks into WAV
    pcm_data = b"".join(audio_chunks)
    wav_data = make_wav_header(len(pcm_data)) + pcm_data
    logger.info(f"Assembled WAV: {len(wav_data)} bytes from {len(audio_chunks)} chunks")

    await _process_and_respond(websocket, session_id, auto_type, wav_data)


async def _handle_batch(websocket, session_id, auto_type):
    """Handle traditional batch protocol (single WAV blob)."""
    while True:
        data = await websocket.receive()

        if data.get("type") == "websocket.disconnect":
            break

        if "bytes" in data:
            audio_data = data["bytes"]
            audio_size = len(audio_data)
            logger.info(f"Batch received: {audio_size} bytes")

            if audio_size < 1000:
                await websocket.send_json({
                    "type": "error",
                    "message": "Audio demasiado corto",
                })
                continue

            await _process_and_respond(websocket, session_id, auto_type, audio_data)


@router.websocket("/dictation/{session_id}")
async def dictation_websocket(
    websocket: WebSocket,
    session_id: str,
    auto_type: bool = Query(False),
    streaming: bool = Query(False),
):
    """
    WebSocket for voice dictation.

    Protocols:
    - Batch (streaming=false): Client sends full WAV, server transcribes.
    - Streaming (streaming=true): Client sends raw PCM chunks, then b"DONE".
      Server assembles WAV and transcribes. Faster because audio uploads
      during recording, not after.
    """
    await websocket.accept()
    logger.info("Dictation WS connected",
                session_id=session_id, auto_type=auto_type, streaming=streaming)

    try:
        if streaming:
            await _handle_streaming(websocket, session_id, auto_type)
        else:
            await _handle_batch(websocket, session_id, auto_type)
    except WebSocketDisconnect:
        logger.info("Dictation WS disconnected", session_id=session_id)
    except Exception as e:
        logger.error("Dictation WS error", error=str(e))
