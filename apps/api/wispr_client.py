"""
Wispr Flow - Voice Dictation
=============================
HOLD Ctrl+Space to record. Release to transcribe and auto-paste.
System audio is muted while recording for clean capture.

Uses Windows RegisterHotKey + GetAsyncKeyState for push-to-talk.
Streams audio chunks to server during recording for faster response.

Usage:  python wispr_client.py
"""
import sys
import os
import subprocess
import time

# ============================================================
# DEPENDENCY CHECK (before any heavy imports)
# ============================================================
def check_dependencies():
    """Check that all required packages are installed."""
    required = {
        'sounddevice': 'sounddevice',
        'numpy': 'numpy',
        'scipy': 'scipy',
        'websockets': 'websockets',
    }
    optional = {
        'pycaw': 'pycaw',
        'comtypes': 'comtypes',
    }
    missing = []
    for module, pip_name in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(pip_name)

    if missing:
        print()
        print("  ==========================================")
        print("    ERROR: Faltan dependencias")
        print("  ==========================================")
        print()
        print(f"  Paquetes faltantes: {', '.join(missing)}")
        print()
        print(f"    pip install {' '.join(missing)}")
        print()
        print("  O ejecuta: instalar_dependencias.bat")
        print()
        input("  Presiona Enter para salir...")
        sys.exit(1)

    for module, pip_name in optional.items():
        try:
            __import__(module)
        except ImportError:
            print(f"  [!] {pip_name} no instalado - mute automatico no disponible")
            print(f"      pip install {pip_name}")

check_dependencies()

# Now safe to import everything
import tkinter as tk
import sounddevice as sd
import numpy as np
import websockets
import asyncio
import threading
import ctypes
import ctypes.wintypes as wintypes
import uuid
import io
import math
import queue
import signal
from scipy.io.wavfile import write as write_wav

# ============================================================
# CONFIG
# ============================================================
API_URL = "ws://localhost:8000/ws/dictation"
API_HTTP = "http://localhost:8000/health"
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_DURATION = 0.5  # seconds per streaming chunk

# Hotkey: Ctrl+Space
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20
VK_CONTROL = 0x11
HOTKEY_ID = 0xBEEF

user32 = ctypes.windll.user32

# GetAsyncKeyState for push-to-talk release detection
GetAsyncKeyState = user32.GetAsyncKeyState
GetAsyncKeyState.argtypes = [ctypes.c_int]
GetAsyncKeyState.restype = ctypes.c_short


# ============================================================
# AUTO-START API SERVER
# ============================================================
def is_api_running():
    try:
        import urllib.request
        req = urllib.request.urlopen(API_HTTP, timeout=2)
        return req.status == 200
    except Exception:
        return False


def start_api_server():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, "wispr_api.log")
    log_file = open(log_path, "w", encoding="utf-8")

    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app",
         "--host", "0.0.0.0", "--port", "8000"],
        cwd=script_dir,
        startupinfo=startupinfo,
        stdout=log_file,
        stderr=log_file,
    )
    return proc, log_path


def show_api_errors(log_path):
    try:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if lines:
                print()
                print("  --- Log de la API ---")
                for line in lines[-15:]:
                    print(f"  {line.rstrip()}")
                print("  --- Fin ---")
    except Exception:
        pass


# ============================================================
# AUDIO MUTER (mute system audio during recording)
# ============================================================
class AudioMuter:
    """
    Mute/unmute system audio during recording.
    Uses pycaw (Windows Core Audio API) if available.
    Falls back to VK_VOLUME_MUTE keypress if not.
    """

    def __init__(self):
        self._was_muted = False
        self._endpoint = None
        self._use_pycaw = False
        self._init()

    def _init(self):
        try:
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(
                IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._endpoint = cast(interface, POINTER(IAudioEndpointVolume))
            self._use_pycaw = True
        except Exception:
            self._use_pycaw = False

    def mute(self):
        if self._use_pycaw and self._endpoint:
            try:
                self._was_muted = bool(self._endpoint.GetMute())
                if not self._was_muted:
                    self._endpoint.SetMute(1, None)
            except Exception:
                pass
        else:
            # Fallback: simulate mute key
            self._was_muted = False
            user32.keybd_event(0xAD, 0, 0, 0)
            user32.keybd_event(0xAD, 0, 0x0002, 0)

    def unmute(self):
        if self._use_pycaw and self._endpoint:
            try:
                if not self._was_muted:
                    self._endpoint.SetMute(0, None)
            except Exception:
                pass
        else:
            if not self._was_muted:
                user32.keybd_event(0xAD, 0, 0, 0)
                user32.keybd_event(0xAD, 0, 0x0002, 0)


# ============================================================
# PUSH-TO-TALK HOTKEY (RegisterHotKey + GetAsyncKeyState)
# ============================================================
class HotkeyListener:
    """
    Push-to-talk: RegisterHotKey detects Ctrl+Space DOWN,
    then polls GetAsyncKeyState to detect release.
    """

    def __init__(self, on_press, on_release):
        self.on_press = on_press
        self.on_release = on_release
        self._running = True
        self.registered = False

    def start(self):
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        return thread

    def _run(self):
        ok = user32.RegisterHotKey(
            None, HOTKEY_ID, MOD_CONTROL | MOD_NOREPEAT, VK_SPACE)
        if not ok:
            print("  [X] ERROR: No se pudo registrar Ctrl+Space")
            print("      Puede que otra app ya lo este usando.")
            return

        self.registered = True
        print("  [OK] Hotkey Ctrl+Space registrado (push-to-talk)")

        msg = wintypes.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            if msg.message == 0x0312 and msg.wParam == HOTKEY_ID:
                threading.Thread(target=self.on_press, daemon=True).start()
                threading.Thread(target=self._poll_release, daemon=True).start()

        user32.UnregisterHotKey(None, HOTKEY_ID)

    def _poll_release(self):
        """Poll until Ctrl or Space is released."""
        time.sleep(0.15)  # avoid false positive on fast key processing
        while self._running:
            ctrl = GetAsyncKeyState(VK_CONTROL) & 0x8000
            space = GetAsyncKeyState(VK_SPACE) & 0x8000
            if not ctrl or not space:
                threading.Thread(target=self.on_release, daemon=True).start()
                return
            time.sleep(0.04)  # 40ms poll

    def stop(self):
        self._running = False
        user32.UnregisterHotKey(None, HOTKEY_ID)


# ============================================================
# SOUND EFFECTS
# ============================================================
class SoundManager:
    @staticmethod
    def _play(start_freq, end_freq, duration_ms):
        try:
            import winsound
            rate = 44100
            t = np.linspace(0, duration_ms / 1000, int(rate * duration_ms / 1000), False)
            freqs = np.linspace(start_freq, end_freq, len(t))
            phases = np.cumsum(freqs) / rate * 2 * np.pi
            wave = np.tanh(np.sin(phases) * np.exp(-10 * t) * 1.5)
            audio = (wave * 32767 * 0.8).astype(np.int16)
            buf = io.BytesIO()
            write_wav(buf, rate, audio)
            buf.seek(0)
            winsound.PlaySound(buf.read(), winsound.SND_MEMORY | winsound.SND_ASYNC)
        except Exception:
            pass

    @staticmethod
    def start():
        SoundManager._play(1000, 500, 100)

    @staticmethod
    def stop():
        SoundManager._play(500, 200, 80)

    @staticmethod
    def success():
        SoundManager._play(600, 1200, 150)

    @staticmethod
    def error():
        SoundManager._play(400, 100, 200)


# ============================================================
# OVERLAY (Siri orb, click-through, never steals focus)
# ============================================================
class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.attributes("-toolwindow", True)

        self.bg = "#1c1c1e"
        self.border = "#3a3a3c"
        self.tkey = "#000001"
        self.root.config(bg=self.tkey)
        self.root.wm_attributes("-transparentcolor", self.tkey)

        self.sz = 70
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{self.sz}x{self.sz}+{(sw-self.sz)//2}+{sh-140}")

        self.cv = tk.Canvas(self.root, width=self.sz, height=self.sz,
                            bg=self.tkey, highlightthickness=0)
        self.cv.pack()
        self._ghost_mode()

        self.anim = None
        self.mode = None
        self.tick = 0
        self.items = []
        self._ang = 0
        self.cv.create_oval(2, 2, self.sz-2, self.sz-2,
                            fill=self.bg, outline=self.border, width=2)

        self.q = queue.Queue()
        self._poll()

    def _ghost_mode(self):
        if os.name != 'nt':
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            st = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            st |= 0x00080000 | 0x00000020 | 0x08000000 | 0x00000008
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, st)
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003)
        except Exception:
            pass

    def _clear(self):
        for i in self.items:
            self.cv.delete(i)
        self.items = []

    def _orb(self):
        self._clear()
        c = self.sz / 2
        self.o1 = self.cv.create_oval(c, c, c, c, fill="#af52de", outline="")
        self.o2 = self.cv.create_oval(c, c, c, c, fill="#5856d6", outline="")
        self.o3 = self.cv.create_oval(c, c, c, c, fill="#00d1fb", outline="")
        self.items.extend([self.o1, self.o2, self.o3])
        self.tick = 0
        self._anim_orb()

    def _anim_orb(self):
        if self.mode != "rec":
            return
        self.tick += 0.08
        c = self.sz / 2
        p = math.sin(self.tick)
        r1, r2, r3 = 20 + p*2, 16 + math.sin(self.tick-0.5)*2, 12 + p
        self.cv.coords(self.o1, c-r1, c-r1, c+r1, c+r1)
        self.cv.coords(self.o2, c-r2, c-r2, c+r2, c+r2)
        self.cv.coords(self.o3, c-r3, c-r3, c+r3, c+r3)
        self.anim = self.root.after(40, self._anim_orb)

    def _spinner(self):
        self._clear()
        self.mode = "spin"
        c, r = self.sz/2, 14
        it = self.cv.create_arc(c-r, c-r, c+r, c+r, start=0, extent=100,
                                style=tk.ARC, outline="#fff", width=3)
        self.items.append(it)
        self._ang = 0
        self._anim_spin(it)

    def _anim_spin(self, it):
        if self.mode != "spin":
            return
        self._ang = (self._ang - 15) % 360
        self.cv.itemconfig(it, start=self._ang)
        self.anim = self.root.after(30, lambda: self._anim_spin(it))

    def _check(self):
        self._clear()
        self.mode = "ok"
        it = self.cv.create_line(30, 45, 40, 55, 60, 35,
                                 capstyle=tk.ROUND, joinstyle=tk.ROUND,
                                 width=4, fill="#32d74b")
        self.items.append(it)

    def _cross(self):
        self._clear()
        self.mode = "err"
        c, r = self.sz/2, 12
        self.items.append(self.cv.create_line(c-r, c-r, c+r, c+r, width=4, fill="#ff453a"))
        self.items.append(self.cv.create_line(c+r, c-r, c-r, c+r, width=4, fill="#ff453a"))

    def _poll(self):
        try:
            while True:
                act, dat = self.q.get_nowait()
                if act == "show":
                    self.root.attributes("-alpha", 1.0)
                    self.root.attributes("-topmost", True)
                    self.root.lift()
                    self.mode = "rec"
                    if self.anim:
                        self.root.after_cancel(self.anim)
                    self._orb()
                elif act == "state":
                    self.root.attributes("-topmost", True)
                    if self.anim:
                        self.root.after_cancel(self.anim)
                    if dat == "spin":
                        self._spinner()
                    elif dat == "ok":
                        self._check()
                    elif dat == "err":
                        self._cross()
                elif act == "hide":
                    self.root.attributes("-alpha", 0.0)
                    self.mode = None
                    if self.anim:
                        self.root.after_cancel(self.anim)
                        self.anim = None
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def show(self):
        self.q.put(("show", None))

    def state(self, s):
        self.q.put(("state", s))

    def hide(self):
        self.q.put(("hide", None))

    def run(self):
        self.root.attributes("-alpha", 1.0)
        self._clear()
        c = self.sz / 2
        dot = self.cv.create_oval(c-5, c-5, c+5, c+5, fill="#32d74b", outline="")
        self.items.append(dot)
        self.root.after(1000, lambda: self.root.attributes("-alpha", 0.0))
        self.root.mainloop()


# ============================================================
# CORE - Push-to-Talk Recording + Streaming + Auto-paste
# ============================================================
class Wispr:
    def __init__(self, overlay):
        self.ui = overlay
        self.recording = False
        self.stream = None
        self._lock = threading.Lock()
        self._chunk_queue = queue.Queue()
        self._muter = AudioMuter()

    def start_recording(self):
        """Called on Ctrl+Space key DOWN (push-to-talk start)."""
        with self._lock:
            if self.recording:
                return
            SoundManager.start()
            time.sleep(0.12)  # let beep play before muting
            self._muter.mute()
            print("  [>>] Grabando... (mantene Ctrl+Space)")
            self.ui.show()
            self.recording = True
            self._chunk_queue = queue.Queue()

            try:
                blocksize = int(SAMPLE_RATE * CHUNK_DURATION)
                self.stream = sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=CHANNELS,
                    dtype='float32', blocksize=blocksize,
                    callback=self._audio_cb)
                self.stream.start()
            except Exception as e:
                print(f"  [!] Error de microfono: {e}")
                self.recording = False
                self._muter.unmute()
                self.ui.hide()
                SoundManager.error()
                return

            # Start WebSocket streaming thread
            threading.Thread(target=self._stream_sender, daemon=True).start()

    def stop_recording(self):
        """Called on Ctrl+Space key RELEASE (push-to-talk stop)."""
        with self._lock:
            if not self.recording:
                return
            print("  [||] Procesando...")
            self.recording = False
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            self._muter.unmute()
            SoundManager.stop()
            self.ui.state("spin")
            # Signal streaming thread that recording is done
            self._chunk_queue.put(None)

    def _audio_cb(self, indata, frames, t, status):
        """Sounddevice callback - converts float32 to int16 PCM bytes."""
        if self.recording:
            pcm = (indata.copy() * 32767).astype(np.int16)
            self._chunk_queue.put(pcm.tobytes())

    def _stream_sender(self):
        """Background thread: streams audio chunks over WebSocket."""
        try:
            asyncio.run(self._async_stream())
        except Exception as e:
            print(f"  [!] Error: {e}")
            self.ui.state("err")
            SoundManager.error()
            time.sleep(1.5)
            self.ui.hide()

    async def _async_stream(self):
        sid = str(uuid.uuid4())
        uri = f"{API_URL}/{sid}?auto_type=true&streaming=true"
        total_bytes = 0
        chunk_count = 0

        try:
            async with websockets.connect(
                uri, ping_timeout=60, close_timeout=10
            ) as ws:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            asyncio.to_thread(self._chunk_queue.get, timeout=0.2),
                            timeout=0.5
                        )
                    except (asyncio.TimeoutError, Exception):
                        # No chunk available yet, keep waiting
                        if not self.recording and self._chunk_queue.empty():
                            # Recording stopped and queue drained
                            await ws.send(b"DONE")
                            break
                        continue

                    if chunk is None:
                        # Sentinel: recording stopped
                        await ws.send(b"DONE")
                        break

                    await ws.send(chunk)
                    chunk_count += 1
                    total_bytes += len(chunk)

                # Check minimum audio
                duration = total_bytes / (SAMPLE_RATE * 2)  # 16-bit = 2 bytes/sample
                if duration < 0.3:
                    print(f"  [!] Muy corto ({duration:.1f}s)")
                    self.ui.hide()
                    return

                print(f"  [~] {duration:.1f}s ({chunk_count} chunks enviados)")

                # Wait for transcript
                resp = await asyncio.wait_for(ws.recv(), timeout=30)
                print(f"  [<] {resp[:200]}")

                self.ui.state("ok")
                SoundManager.success()
                await asyncio.sleep(0.5)
                self.ui.hide()

        except asyncio.TimeoutError:
            print("  [!] Timeout")
            self.ui.state("err")
            SoundManager.error()
            await asyncio.sleep(1.5)
            self.ui.hide()
        except Exception as e:
            print(f"  [!] {e}")
            self.ui.state("err")
            SoundManager.error()
            await asyncio.sleep(1.5)
            self.ui.hide()


# ============================================================
# MAIN
# ============================================================
def main():
    os.system("title Wispr Flow - Voice Dictation")
    os.system("color 0A")

    print()
    print("  ==========================================")
    print("       WISPR FLOW - Voice Dictation")
    print("  ==========================================")
    print()

    # --- Step 1: Dependencies ---
    print("  [1/4] Dependencias OK")

    # --- Step 2: Audio muter ---
    muter = AudioMuter()
    if muter._use_pycaw:
        print("  [2/4] Mute automatico activado (pycaw)")
    else:
        print("  [2/4] Mute automatico: modo basico (instala pycaw para mejor)")

    # --- Step 3: Auto-start API ---
    print("  [3/4] Verificando API server...")
    api_proc = None
    log_path = None
    if is_api_running():
        print("  [OK] API corriendo en puerto 8000")
    else:
        print("  [..] Arrancando API server...")
        api_proc, log_path = start_api_server()
        for i in range(20):
            time.sleep(1)
            if is_api_running():
                break
            if i < 5:
                print(f"       Esperando... ({i+1}s)")
        if is_api_running():
            print("  [OK] API lista!")
        else:
            print("  [!] API no arranco")
            if log_path:
                show_api_errors(log_path)
            print("      python -m uvicorn src.main:app --port 8000")

    # --- Step 4: Hotkey ---
    print("  [4/4] Registrando hotkey...")
    overlay = Overlay()
    wispr = Wispr(overlay)
    hotkey = HotkeyListener(
        on_press=wispr.start_recording,
        on_release=wispr.stop_recording,
    )
    hotkey.start()
    time.sleep(0.5)

    print()
    print("  ==========================================")
    if hotkey.registered:
        print("      LISTO! Push-to-Talk activo")
    else:
        print("      ERROR: Hotkey no se registro")
    print("  ==========================================")
    print()
    print("  COMO USAR:")
    print("  1. Clickea en cualquier campo de texto")
    print("  2. MANTENE presionado  Ctrl + Espacio")
    print("  3. Habla mientras mantenes presionado")
    print("  4. SOLTA para transcribir y pegar!")
    print()
    print("  La musica se mutea al grabar.")
    print("  Minimiza esta ventana (no la cierres).")
    print()
    print("  ------------------------------------------")
    print("  Esperando Ctrl+Space...")
    print("  ------------------------------------------")
    print()

    try:
        overlay.run()
    except KeyboardInterrupt:
        pass
    finally:
        if api_proc:
            api_proc.terminate()
        hotkey.stop()


if __name__ == "__main__":
    main()
