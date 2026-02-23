"""
Wispr Flow - Voice Dictation
=============================
Press Ctrl+Space anywhere to dictate. Text auto-pastes into the active field.

Uses Windows RegisterHotKey API (official, 100% reliable).
Starts the API server automatically if not running.

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
        print("  Para instalarlos, ejecuta este comando:")
        print()
        print(f"    pip install {' '.join(missing)}")
        print()
        print("  O ejecuta: instalar_dependencias.bat")
        print()
        print("  ==========================================")
        print()
        input("  Presiona Enter para salir...")
        sys.exit(1)

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

# Hotkey: Ctrl+Space (RegisterHotKey)
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20
HOTKEY_ID = 0xBEEF

user32 = ctypes.windll.user32


# ============================================================
# AUTO-START API SERVER
# ============================================================
def is_api_running():
    """Check if the API server is already running."""
    try:
        import urllib.request
        req = urllib.request.urlopen(API_HTTP, timeout=2)
        return req.status == 200
    except Exception:
        return False


def start_api_server():
    """Start the FastAPI server in the background with visible error logging."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, "wispr_api.log")

    # Log file for API errors (so we can show them if it fails)
    log_file = open(log_path, "w", encoding="utf-8")

    # Start uvicorn as a subprocess
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE

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
    """Show the last lines of the API log file if it failed to start."""
    try:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if lines:
                print()
                print("  --- Ultimas lineas del log de la API ---")
                for line in lines[-15:]:
                    print(f"  {line.rstrip()}")
                print("  --- Fin del log ---")
                print()
    except Exception:
        pass


# ============================================================
# HOTKEY (Windows RegisterHotKey - official & bulletproof)
# ============================================================
class HotkeyListener:
    """
    Uses RegisterHotKey API to capture Ctrl+Space system-wide.
    This is the same API used by Discord, OBS, Spotify, etc.
    100% reliable, no admin required.
    """

    def __init__(self, callback):
        self.callback = callback
        self._running = True
        self.registered = False

    def start(self):
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        return thread

    def _run(self):
        # Register Ctrl+Space
        ok = user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_NOREPEAT, VK_SPACE)
        if not ok:
            print("  [X] ERROR: No se pudo registrar Ctrl+Space")
            print("      Puede que otra app ya lo este usando.")
            print("      Cerrala e intenta de nuevo.")
            return

        self.registered = True
        print("  [OK] Hotkey Ctrl+Space registrado!")

        # Message pump - wait for hotkey events
        msg = wintypes.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            if msg.message == 0x0312:  # WM_HOTKEY
                if msg.wParam == HOTKEY_ID:
                    threading.Thread(target=self.callback, daemon=True).start()

        user32.UnregisterHotKey(None, HOTKEY_ID)

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
# UI OVERLAY (Siri orb, click-through, never steals focus)
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
        # Brief startup flash
        self.root.attributes("-alpha", 1.0)
        self._clear()
        c = self.sz / 2
        dot = self.cv.create_oval(c-5, c-5, c+5, c+5, fill="#32d74b", outline="")
        self.items.append(dot)
        self.root.after(1000, lambda: self.root.attributes("-alpha", 0.0))
        self.root.mainloop()


# ============================================================
# CORE - Recording + Transcription + Auto-paste
# ============================================================
class Wispr:
    def __init__(self, overlay):
        self.ui = overlay
        self.recording = False
        self.buf = []
        self.stream = None
        self._lock = threading.Lock()

    def toggle(self):
        if self.recording:
            self._stop()
        else:
            self._start()

    def _start(self):
        with self._lock:
            if self.recording:
                return
            print("  [>>] Grabando... (habla ahora)")
            SoundManager.start()
            self.ui.show()
            self.recording = True
            self.buf = []
            try:
                self.stream = sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=CHANNELS,
                    dtype='float32', callback=self._cb)
                self.stream.start()
            except Exception as e:
                print(f"  [!] Error de microfono: {e}")
                self.recording = False
                self.ui.hide()
                SoundManager.error()

    def _stop(self):
        with self._lock:
            if not self.recording:
                return
            print("  [||] Procesando audio...")
            SoundManager.stop()
            self.ui.state("spin")
            self.recording = False
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            chunks = list(self.buf)
            self.buf = []
        threading.Thread(target=self._send, args=(chunks,), daemon=True).start()

    def _cb(self, indata, frames, t, status):
        if self.recording:
            self.buf.append(indata.copy())

    def _send(self, chunks):
        try:
            asyncio.run(self._async_send(chunks))
        except Exception as e:
            print(f"  [!] Error: {e}")
            self.ui.state("err")
            SoundManager.error()
            time.sleep(1.5)
            self.ui.hide()

    async def _async_send(self, chunks):
        if not chunks:
            self.ui.hide()
            return

        audio = np.concatenate(chunks, axis=0)
        dur = len(audio) / SAMPLE_RATE
        if dur < 0.3:
            print(f"  [!] Muy corto ({dur:.1f}s)")
            self.ui.hide()
            return

        print(f"  [~] {dur:.1f}s de audio")
        pcm = (audio * 32767).astype(np.int16)
        wav = io.BytesIO()
        write_wav(wav, SAMPLE_RATE, pcm)
        data = wav.getvalue()

        sid = str(uuid.uuid4())
        uri = f"{API_URL}/{sid}?auto_type=true"

        try:
            async with websockets.connect(uri, ping_timeout=60, close_timeout=5) as ws:
                await ws.send(data)
                print(f"  [>] Enviado {len(data)} bytes")
                resp = await asyncio.wait_for(ws.recv(), timeout=30)
                print(f"  [<] {resp[:150]}")

                self.ui.state("ok")
                SoundManager.success()
                await asyncio.sleep(0.7)
                self.ui.hide()

        except asyncio.TimeoutError:
            print("  [!] Timeout esperando transcripcion")
            self.ui.state("err")
            SoundManager.error()
            await asyncio.sleep(1.5)
            self.ui.hide()
        except Exception as e:
            print(f"  [!] Error de conexion: {e}")
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
    print("  [1/3] Verificando dependencias...")
    print("  [OK] Todas las dependencias instaladas")
    print()

    # --- Step 2: Auto-start API ---
    print("  [2/3] Verificando API server...")
    api_proc = None
    log_path = None
    if is_api_running():
        print("  [OK] API ya esta corriendo en puerto 8000")
    else:
        print("  [..] Arrancando API server...")
        api_proc, log_path = start_api_server()
        # Wait for it
        for i in range(20):
            time.sleep(1)
            if is_api_running():
                break
            if i < 5:
                print(f"       Esperando... ({i+1}s)")
        if is_api_running():
            print("  [OK] API lista en puerto 8000")
        else:
            print("  [!] API no arranco despues de 20 segundos")
            if log_path:
                show_api_errors(log_path)
            print("  [!] Ejecuta manualmente:")
            print("      python -m uvicorn src.main:app --port 8000")
            print()
            print("  El dictado NO funcionara sin la API.")
            print()
    print()

    # --- Step 3: Hotkey ---
    print("  [3/3] Registrando hotkey...")
    overlay = Overlay()
    wispr = Wispr(overlay)
    hotkey = HotkeyListener(callback=wispr.toggle)
    hotkey.start()

    # Wait a moment for hotkey registration
    time.sleep(0.5)

    print()
    print("  ==========================================")
    if hotkey.registered:
        print("     LISTO! Ya podes dictar por voz")
    else:
        print("     ERROR: Hotkey no se pudo registrar")
    print("  ==========================================")
    print()
    print("  COMO USAR:")
    print("  1. Clickea en cualquier campo de texto")
    print("     (Chrome, Word, WhatsApp Web, VS Code, etc)")
    print("  2. Presiona  Ctrl + Espacio")
    print("  3. Habla (vas a ver un circulo animado)")
    print("  4. Presiona  Ctrl + Espacio  de nuevo")
    print("  5. El texto se pega automaticamente!")
    print()
    print("  Podes minimizar esta ventana.")
    print("  NO la cierres o se desactiva el dictado.")
    print()
    print("  ------------------------------------------")
    print("  Esperando Ctrl+Space...")
    print("  ------------------------------------------")
    print()

    # --- Run UI loop ---
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
