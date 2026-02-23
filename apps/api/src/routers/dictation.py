"""
Dictation WebSocket Router
===========================
Handles voice dictation with auto-type capability.
Receives audio -> transcribes -> optionally types into the active text field.
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
import structlog
import asyncio
import time
import pyautogui
import pyperclip

from src.services.asr import transcribe_audio

router = APIRouter()
logger = structlog.get_logger()

# Configure pyautogui
pyautogui.FAILSAFE = False


def type_text_via_clipboard(text: str):
    """
    Type text by copying to clipboard and pressing Ctrl+V.

    Most reliable method for typing into ANY text field on Windows:
    browsers, IDEs, terminals, Office, etc.

    Robustness measures:
    - Release ALL modifier keys first (prevents Ctrl+Shift+V issues)
    - Wait for clipboard to actually update
    - Clean Ctrl+V press
    """
    try:
        # Step 1: Force release ALL modifier keys
        # Critical: if user just released Ctrl+Win hotkey, Windows might
        # still have modifiers in "pressed" state
        for key in ['shift', 'ctrl', 'alt', 'win', 'winleft', 'winright']:
            try:
                pyautogui.keyUp(key)
            except Exception:
                pass

        # Step 2: Wait for modifier keys to fully release
        time.sleep(0.15)

        # Step 3: Copy text to clipboard
        pyperclip.copy(text)

        # Step 4: Wait for clipboard to update (Windows can be slow)
        time.sleep(0.25)

        # Step 5: Simulate Ctrl+V paste
        pyautogui.hotkey('ctrl', 'v')

        # Step 6: Small delay to let paste complete
        time.sleep(0.1)

        logger.info("Auto-type completed", chars=len(text))

    except Exception as e:
        logger.error("Auto-type failed", error=str(e))


@router.websocket("/dictation/{session_id}")
async def dictation_websocket(
    websocket: WebSocket,
    session_id: str,
    auto_type: bool = Query(False),
):
    """
    WebSocket for voice dictation.

    Flow:
    1. Client connects
    2. Client sends audio bytes (WAV or WebM - auto-detected)
    3. Server transcribes audio via Whisper/Deepgram
    4. Server sends back transcript JSON
    5. If auto_type=true, server pastes text into active window

    Params:
    - session_id: Unique session identifier
    - auto_type: If true, simulates Ctrl+V to paste text
    """
    await websocket.accept()
    logger.info("Dictation WS connected", session_id=session_id, auto_type=auto_type)

    try:
        while True:
            data = await websocket.receive()

            # Check for disconnect
            if data.get("type") == "websocket.disconnect":
                logger.info("Dictation client disconnected", session_id=session_id)
                break

            if "bytes" in data:
                audio_data = data["bytes"]
                audio_size = len(audio_data)
                logger.info(f"Dictation received audio: {audio_size} bytes")

                # Skip too-small audio (noise/accidental press)
                if audio_size < 1000:
                    logger.warning("Audio too short, skipping", bytes=audio_size)
                    await websocket.send_json({
                        "type": "error",
                        "message": "Audio demasiado corto",
                    })
                    continue

                try:
                    # Transcribe (format auto-detected from magic bytes)
                    transcript = await transcribe_audio(audio_data, delayed=True)

                    # Custom corrections
                    if transcript:
                        transcript = transcript.replace("Vale", "Dale").replace("vale", "dale")

                    if transcript and transcript.strip():
                        logger.info(f"Dictation transcript: '{transcript[:100]}'")

                        # Send transcript to client
                        await websocket.send_json({
                            "type": "transcript",
                            "text": transcript,
                            "session_id": session_id,
                        })

                        # Auto-type if enabled
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
                    logger.error("Dictation transcription error", error=str(e))
                    await websocket.send_json({
                        "type": "error",
                        "message": str(e),
                    })

    except WebSocketDisconnect:
        logger.info("Dictation WS disconnected", session_id=session_id)
    except Exception as e:
        logger.error("Dictation WS error", error=str(e))
