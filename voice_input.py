"""
豆包 Seed ASR 语音输入法
========================
按下快捷键开始录音，松手自动识别并输入文本

使用前需在火山引擎控制台 (https://console.volcengine.com/doubao/voice) 创建应用获取:
  - APP ID (app_key)
  - Access Token (access_key)
  (Resource ID 已内置为 volc.bigasr.sauc.duration)
"""

import asyncio
import json
import gzip
import struct
import uuid
import threading
import queue
import os
import time
import logging
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Callable

import numpy as np
import sounddevice as sd
import pyperclip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("voice_input")

# ── Audio constants ──
SAMPLE_RATE = 16000
CHANNELS = 1
BITS = 16
DTYPE = np.int16
FRAME_DURATION_MS = 200  # 200ms per WebSocket audio packet

# ── WebSocket binary protocol constants ──
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
ERROR_TYPE = 0b1111

FLAG_NO_SEQUENCE = 0b0000
FLAG_POS_SEQUENCE = 0b0001
FLAG_NO_SEQ_LAST = 0b0010
FLAG_NEG_SEQUENCE = 0b0011

SERIALIZATION_NONE = 0b0000
SERIALIZATION_JSON = 0b0001
COMPRESSION_NONE = 0b0000
COMPRESSION_GZIP = 0b0001

WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
RESOURCE_ID = "volc.seedasr.sauc.duration"

# ── Config path ──
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "voice-input"
CONFIG_FILE = CONFIG_DIR / "config.json"
os.makedirs(CONFIG_DIR, exist_ok=True)

# ── Hotkey name normalization map ──
HOTKEY_DISPLAY_NAMES = {
    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
    "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8",
    "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
    "ctrl": "Ctrl", "shift": "Shift", "alt": "Alt",
    "space": "空格", "enter": "Enter", "tab": "Tab",
    "caps_lock": "CapsLock",
}


# ══════════════════════════════════════════════
#  Protocol helpers
# ══════════════════════════════════════════════

def make_header(msg_type: int, flags: int = FLAG_POS_SEQUENCE,
                serialization: int = SERIALIZATION_JSON,
                compression: int = COMPRESSION_GZIP) -> bytes:
    """Build the 4-byte WebSocket binary protocol header.

    Byte 0: [7:4] protocol version=1, [3:0] header size=1 (4 bytes)
    Byte 1: [7:4] message type, [3:0] flags
    Byte 2: [7:4] serialization, [3:0] compression
    Byte 3: reserved = 0
    """
    return struct.pack(
        "!BBBB",
        (0b0001 << 4) | 0b0001,        # version=1, header_size=1
        (msg_type << 4) | flags,        # type + flags
        (serialization << 4) | compression,  # serialization + compression
        0x00,
    )


def pack_full_client_request(params: dict, seq: int = 1) -> bytes:
    """Pack a FullClientRequest with gzip-compressed JSON payload.
    Matches official demo: signed int seq, JSON+GZIP serialization."""
    payload = gzip.compress(json.dumps(params, ensure_ascii=False).encode("utf-8"))
    header = make_header(FULL_CLIENT_REQUEST, FLAG_POS_SEQUENCE,
                         SERIALIZATION_JSON, COMPRESSION_GZIP)
    return header + struct.pack(">i", seq) + struct.pack(">I", len(payload)) + payload


def pack_audio_chunk(pcm_data: bytes, seq: int, is_last: bool = False) -> bytes:
    """Pack an AudioOnlyRequest with gzip-compressed PCM payload.
    Matches official demo: JSON+GZIP serialization for all messages."""
    payload = gzip.compress(pcm_data)
    flags = FLAG_NEG_SEQUENCE if is_last else FLAG_POS_SEQUENCE
    header = make_header(AUDIO_ONLY_REQUEST, flags,
                         SERIALIZATION_JSON, COMPRESSION_GZIP)
    seq_val = -seq if is_last else seq
    return header + struct.pack(">i", seq_val) + struct.pack(">I", len(payload)) + payload


def parse_response(data: bytes) -> tuple[Optional[dict], bool]:
    """Parse a binary WebSocket response (matches official demo format).
    Returns (parsed_dict, is_final) or (error_dict, True) on error.
    """
    if len(data) < 4:
        return None, True

    header = data[:4]
    msg_type = (header[1] >> 4) & 0x0f
    flags = header[1] & 0x0f
    compression = header[2] & 0x0f

    offset = 4
    # flags & 0x01 → has sequence number
    if flags & 0x01:
        offset += 4
    # flags & 0x04 → has event/code field before payload
    if flags & 0x04:
        offset += 4

    # Error message format
    if msg_type == ERROR_TYPE:
        code = struct.unpack(">i", data[offset:offset + 4])[0]
        payload_size = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        err_msg = data[offset + 8:offset + 8 + payload_size].decode("utf-8", errors="replace")
        log.error(f"Server error: code={code}, msg={err_msg}")
        return {"error": True, "code": code, "message": err_msg}, True

    if msg_type != FULL_SERVER_RESPONSE:
        return None, True

    payload_size = struct.unpack(">I", data[offset:offset + 4])[0]
    offset += 4
    if offset + payload_size > len(data):
        return None, True

    raw = data[offset:offset + payload_size]
    try:
        if compression == COMPRESSION_GZIP:
            raw = gzip.decompress(raw)
        # Strip any non-JSON prefix
        start = raw.find(b'{')
        if start > 0:
            raw = raw[start:]
        result = json.loads(raw.decode("utf-8"))
        return result, bool(flags & 0x02)
    except Exception as e:
        log.error(f"Parse error: {e}")
        return None, True


# ══════════════════════════════════════════════
#  Audio recorder
# ══════════════════════════════════════════════

class AudioRecorder:
    """Records PCM audio from microphone into a thread-safe buffer."""

    def __init__(self):
        self._buffer: list[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._recording = False
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._recording = True

        def callback(indata, frames, time_info, status):
            if status:
                log.warning(f"Audio callback status: {status}")
            if self._recording:
                with self._lock:
                    self._buffer.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=int(SAMPLE_RATE * FRAME_DURATION_MS / 1000),
            callback=callback,
        )
        self._stream.start()
        log.info("Recording started")

    def stop(self) -> Optional[bytes]:
        """Stop recording and return concatenated PCM bytes, or None if empty."""
        self._recording = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        with self._lock:
            if not self._buffer:
                log.info("No audio recorded")
                return None
            audio = np.concatenate(self._buffer, axis=0).tobytes()
            duration = len(audio) / (SAMPLE_RATE * 2)  # 16-bit = 2 bytes per sample
            log.info(f"Recording stopped: {len(self._buffer)} chunks, "
                     f"{len(audio)} bytes ({duration:.1f}s)")
            return audio


# ══════════════════════════════════════════════
#  Seed ASR WebSocket client
# ══════════════════════════════════════════════

@dataclass
class ASRConfig:
    app_id: str = ""
    access_key: str = ""

    def is_valid(self) -> bool:
        return bool(self.app_id and self.access_key)


class SeedASRClient:
    """WebSocket client for 火山引擎 Seed ASR (流式输入模式)."""

    def __init__(self, config: ASRConfig):
        self.config = config

    async def transcribe(self, pcm_data: bytes) -> str:
        """Send PCM audio and return transcribed text."""
        if not self.config.is_valid():
            raise ValueError("API credentials not configured")

        headers = {
            "X-Api-App-Key": self.config.app_id,
            "X-Api-Access-Key": self.config.access_key,
            "X-Api-Resource-Id": RESOURCE_ID,
            "X-Api-Request-Id": str(uuid.uuid4()),
        }

        log.info(f"Connecting to Seed ASR...")
        import websockets
        async with websockets.connect(
            WS_URL,
            additional_headers=list(headers.items()),
            ping_interval=None,
        ) as ws:
            # ── 1. Send FullClientRequest ──
            request_params = {
                "user": {"uid": "voice_input"},
                "audio": {
                    "format": "pcm",
                    "rate": SAMPLE_RATE,
                    "bits": BITS,
                    "channel": CHANNELS,
                },
                "request": {
                    "model_name": "bigmodel",
                    "enable_punc": True,
                    "enable_itn": True,
                    "enable_ddc": True,
                },
            }
            await ws.send(pack_full_client_request(request_params, seq=1))
            log.info("FullClientRequest sent")

            # Read welcome / task-started response
            resp = await ws.recv()
            if isinstance(resp, bytes):
                body, _ = parse_response(resp)
                if body:
                    log.info(f"Server welcome: {json.dumps(body, ensure_ascii=False)[:200]}")

            # ── 2. Send audio chunks ──
            chunk_bytes = int(SAMPLE_RATE * 2 * FRAME_DURATION_MS / 1000)  # 200ms
            total = len(pcm_data)
            offset = 0
            seq = 2

            while offset < total:
                chunk = pcm_data[offset:offset + chunk_bytes]
                is_last = (offset + chunk_bytes >= total)
                await ws.send(pack_audio_chunk(chunk, seq, is_last))
                seq += 1
                offset += chunk_bytes

            log.info(f"Sent {seq - 2} audio chunks (last={is_last})")

            # ── 3. Receive results ──
            full_text = ""
            while True:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=30.0)
                except asyncio.TimeoutError:
                    log.warning("Receive timeout (30s), using last result")
                    break
                except websockets.exceptions.ConnectionClosed as e:
                    log.info(f"Connection closed: {e.code}")
                    break

                if isinstance(message, bytes):
                    body, is_final = parse_response(message)
                    if body is None:
                        continue
                    if body.get("error"):
                        raise RuntimeError(body.get("message", "Unknown server error"))
                    try:
                        text = body["result"]["text"]
                    except (KeyError, TypeError):
                        text = ""
                    if text:
                        full_text = text
                        log.info(f"Received: {text[:80]}")
                    if is_final:
                        log.info(f"Final result: {full_text[:100]}")
                        break

            return full_text


# ══════════════════════════════════════════════
#  Background asyncio engine
# ══════════════════════════════════════════════

class AsyncEngine:
    """Runs an asyncio event loop in a daemon background thread."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, callback: Optional[Callable] = None):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if callback:
            future.add_done_callback(callback)
        return future

    def shutdown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


# ══════════════════════════════════════════════
#  Hotkey -> pynput mapping
# ══════════════════════════════════════════════

def parse_hotkey(name: str):
    """Convert a hotkey string (e.g. 'f2') to a pynput key object."""
    from pynput import keyboard as kb
    name = name.lower().strip()
    mapping = {
        "f1": kb.Key.f1, "f2": kb.Key.f2, "f3": kb.Key.f3, "f4": kb.Key.f4,
        "f5": kb.Key.f5, "f6": kb.Key.f6, "f7": kb.Key.f7, "f8": kb.Key.f8,
        "f9": kb.Key.f9, "f10": kb.Key.f10, "f11": kb.Key.f11, "f12": kb.Key.f12,
        "space": kb.Key.space, "enter": kb.Key.enter, "tab": kb.Key.tab,
        "esc": kb.Key.esc, "backspace": kb.Key.backspace,
        "delete": kb.Key.delete, "home": kb.Key.home, "end": kb.Key.end,
        "page_up": kb.Key.page_up, "page_down": kb.Key.page_down,
        "insert": kb.Key.insert,
        "ctrl": kb.Key.ctrl, "ctrl_l": kb.Key.ctrl_l, "ctrl_r": kb.Key.ctrl_r,
        "shift": kb.Key.shift, "shift_l": kb.Key.shift_l, "shift_r": kb.Key.shift_r,
        "alt": kb.Key.alt, "alt_l": kb.Key.alt_l, "alt_r": kb.Key.alt_r,
        "cmd": kb.Key.cmd, "cmd_l": kb.Key.cmd_l, "cmd_r": kb.Key.cmd_r,
    }
    if name in mapping:
        return mapping[name]
    # Single character key
    if len(name) == 1:
        return kb.KeyCode.from_char(name)
    raise ValueError(f"Unknown hotkey: {name}")


# ══════════════════════════════════════════════
#  Main Application
# ══════════════════════════════════════════════

class VoiceInputApp:
    STATES = {
        "idle": "就绪",
        "recording": "录音中...",
        "transcribing": "识别中...",
        "error": "出错",
    }

    def __init__(self):
        # Load config
        self.config_data = self._load_config()
        self.asr_config = ASRConfig(
            app_id=self.config_data.get("app_id", ""),
            access_key=self.config_data.get("access_key", ""),
        )
        self.hotkey_name = self.config_data.get("hotkey", "f2")

        # State
        self.state = "idle"
        self._pressed = False
        self._processing = False  # guards against double-trigger

        # Components
        self.recorder = AudioRecorder()
        self.asr_client = SeedASRClient(self.asr_config)
        self.async_engine = AsyncEngine()

        # GUI
        self.root: Optional[tk.Tk] = None
        self._status_var: Optional[tk.StringVar] = None
        self._hotkey_var: Optional[tk.StringVar] = None
        self._tray_icon = None

        # Hotkey listener
        self._pynput_listener: Optional[threading.Thread] = None
        self._hotkey_activated = False

        # Build GUI
        self._build_gui()

        # Start hotkey listener
        self._start_hotkey_listener()

        # Protocol-safe shutdown
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        log.info("Voice Input App started")

    # ── Config persistence ──

    def _load_config(self) -> dict:
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            log.warning(f"Failed to load config: {e}")
        return {"hotkey": "f2"}

    def _save_config(self):
        try:
            self.config_data["app_id"] = self.asr_config.app_id
            self.config_data["access_key"] = self.asr_config.access_key
            self.config_data["hotkey"] = self.hotkey_name
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Failed to save config: {e}")

    # ── GUI ──

    def _build_gui(self):
        self.root = tk.Tk()
        self.root.title("语音输入法")
        self.root.geometry("360x280")
        self.root.resizable(False, False)
        self.root.configure(bg="#f5f5f5")

        # Center window
        self.root.update_idletasks()
        w, h = 360, 280
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

        # Icon / title bar
        try:
            img = tk.Image("photo", file=self._create_icon_data())
            self.root.iconphoto(True, img)
        except Exception:
            pass

        main_frame = tk.Frame(self.root, bg="#f5f5f5", padx=20, pady=15)
        main_frame.pack(fill="both", expand=True)

        # ── Title ──
        title = tk.Label(main_frame, text="🎤 语音输入法",
                         font=("Microsoft YaHei", 16, "bold"),
                         bg="#f5f5f5", fg="#333")
        title.pack(pady=(0, 10))

        # ── Status ──
        status_frame = tk.Frame(main_frame, bg="#f5f5f5")
        status_frame.pack(fill="x", pady=5)
        tk.Label(status_frame, text="状态:", font=("Microsoft YaHei", 10),
                 bg="#f5f5f5", fg="#555").pack(side="left")
        self._status_var = tk.StringVar(value=self.STATES["idle"])
        status_label = tk.Label(status_frame, textvariable=self._status_var,
                                font=("Microsoft YaHei", 10, "bold"),
                                bg="#f5f5f5", fg="#2ecc71")
        status_label.pack(side="left", padx=(8, 0))

        # ── Hotkey display (clickable) ──
        hotkey_frame = tk.Frame(main_frame, bg="#f5f5f5")
        hotkey_frame.pack(fill="x", pady=5)
        tk.Label(hotkey_frame, text="快捷键:", font=("Microsoft YaHei", 10),
                 bg="#f5f5f5", fg="#555").pack(side="left")
        self._hotkey_var = tk.StringVar(value=HOTKEY_DISPLAY_NAMES.get(self.hotkey_name, self.hotkey_name.upper()))
        hotkey_label = tk.Label(hotkey_frame, textvariable=self._hotkey_var,
                                font=("Microsoft YaHei", 12, "bold"),
                                bg="#f5f5f5", fg="#2980b9", cursor="hand2")
        hotkey_label.pack(side="left", padx=(8, 0))
        hotkey_label.bind("<Button-1>", lambda e: self._open_hotkey_picker())

        # ── Separator ──
        tk.Frame(main_frame, bg="#ddd", height=1).pack(fill="x", pady=10)

        # ── Buttons ──
        btn_frame = tk.Frame(main_frame, bg="#f5f5f5")
        btn_frame.pack(fill="x", pady=5)

        btn_style = {"font": ("Microsoft YaHei", 9), "padx": 12, "pady": 4,
                     "border": 0, "cursor": "hand2"}

        tk.Button(btn_frame, text="⚙ 设置", bg="#3498db", fg="white",
                  command=self._open_settings, **btn_style).pack(side="left", padx=5)
        tk.Button(btn_frame, text="📋 测试", bg="#2ecc71", fg="white",
                  command=self._test_api, **btn_style).pack(side="left", padx=5)
        tk.Button(btn_frame, text="❓ 帮助", bg="#95a5a6", fg="white",
                  command=self._show_help, **btn_style).pack(side="left", padx=5)

        # ── Hint text ──
        hint = tk.Label(main_frame,
                        text=f"按住 {HOTKEY_DISPLAY_NAMES.get(self.hotkey_name, self.hotkey_name.upper())} 开始录音\n"
                             f"松开自动识别并输入",
                        font=("Microsoft YaHei", 9), bg="#f5f5f5",
                        fg="#999", justify="center")
        hint.pack(pady=(15, 0))

    def _set_state(self, state: str):
        self.state = state
        if self._status_var:
            text = self.STATES.get(state, state)
            self._status_var.set(text)
            # Color
            colors = {"idle": "#2ecc71", "recording": "#e74c3c",
                      "transcribing": "#f39c12", "error": "#e74c3c"}
            color = colors.get(state, "#333")
            # Find the status label (hacky but works for simple UI)
            for w in self.root.winfo_children():
                for c in w.winfo_children():
                    if isinstance(c, tk.Label) and c.cget("textvariable") == str(self._status_var):
                        c.configure(fg=color)

    def _create_icon_data(self):
        """Create a simple 16x16 PNG icon for the window."""
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([8, 8, 56, 56], fill="#3498db")
            draw.ellipse([20, 20, 44, 44], fill="white")
            draw.polygon([(26, 24), (26, 40), (40, 32)], fill="#3498db")
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except ImportError:
            return None

    # ── Hotkey picker dialog ──

    def _open_hotkey_picker(self):
        from pynput import keyboard as kb

        self._pressed = False
        # Stop current listener
        if self._pynput_listener:
            try: self._pynput_listener.stop()
            except Exception: pass

        captured = [None]  # mutable cell

        dialog = tk.Toplevel(self.root)
        dialog.title("设置快捷键")
        dialog.geometry("360x180")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg="#f5f5f5")

        frame = tk.Frame(dialog, bg="#f5f5f5", padx=20, pady=20)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="请按下新快捷键...", font=("Microsoft YaHei", 12),
                 bg="#f5f5f5", fg="#555").pack(pady=(0, 15))

        key_display = tk.Label(frame, text="等待按键...", font=("Microsoft YaHei", 18, "bold"),
                               bg="#f0f0f0", fg="#2980b9", relief="solid", width=20, height=2)
        key_display.pack(pady=5)

        def key_to_name(key):
            mapping = {
                kb.Key.f1: "F1", kb.Key.f2: "F2", kb.Key.f3: "F3", kb.Key.f4: "F4",
                kb.Key.f5: "F5", kb.Key.f6: "F6", kb.Key.f7: "F7", kb.Key.f8: "F8",
                kb.Key.f9: "F9", kb.Key.f10: "F10", kb.Key.f11: "F11", kb.Key.f12: "F12",
                kb.Key.space: "空格", kb.Key.enter: "Enter", kb.Key.tab: "Tab",
                kb.Key.esc: "Esc", kb.Key.backspace: "Backspace",
                kb.Key.delete: "Delete", kb.Key.home: "Home", kb.Key.end: "End",
                kb.Key.page_up: "PageUp", kb.Key.page_down: "PageDown",
                kb.Key.insert: "Insert",
            }
            if key in mapping:
                return mapping[key], None
            if hasattr(key, 'char') and key.char:
                return key.char.upper(), key.char.lower()
            return None, None

        def on_press(key):
            if captured[0] is not None:
                return
            display, name_key = key_to_name(key)
            if display:
                captured[0] = name_key or display.lower()
                key_display.config(text=display)
            return False  # stop listener

        listener = kb.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()

        def on_destroy():
            if listener and listener.running:
                listener.stop()
            if captured[0] and captured[0] != self.hotkey_name:
                self.hotkey_name = captured[0]
                self._hotkey_var.set(HOTKEY_DISPLAY_NAMES.get(captured[0], captured[0].upper()))
                for w in self.root.winfo_children():
                    for c in w.winfo_children():
                        if isinstance(c, tk.Label) and "按住" in c.cget("text"):
                            c.configure(text=f"按住 {HOTKEY_DISPLAY_NAMES.get(captured[0], captured[0].upper())} 开始录音\n松开自动识别并输入")
                self._save_config()
            self._start_hotkey_listener()

        def confirm():
            on_destroy()
            dialog.destroy()

        btn_frame = tk.Frame(frame, bg="#f5f5f5")
        btn_frame.pack(pady=(10, 0))

        tk.Button(btn_frame, text="确定", bg="#3498db", fg="white",
                  font=("Microsoft YaHei", 9), padx=20,
                  command=confirm).pack(side="left", padx=5)

        dialog.protocol("WM_DELETE_WINDOW", on_destroy)
        dialog.wait_window()

    # ── Settings dialog ──

    def _open_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("设置")
        dialog.geometry("420x370")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg="#f5f5f5")

        frame = tk.Frame(dialog, bg="#f5f5f5", padx=20, pady=15)
        frame.pack(fill="both", expand=True)

        row = 0

        # App ID
        tk.Label(frame, text="APP ID:", bg="#f5f5f5", font=("Microsoft YaHei", 10),
                 anchor="w").grid(row=row, column=0, sticky="w", pady=5)
        app_id_entry = tk.Entry(frame, width=35, font=("Consolas", 10))
        app_id_entry.insert(0, self.asr_config.app_id)
        app_id_entry.grid(row=row, column=0, columnspan=2, pady=2, sticky="ew")
        row += 1

        # Access Key
        tk.Label(frame, text="Access Token:", bg="#f5f5f5", font=("Microsoft YaHei", 10),
                 anchor="w").grid(row=row, column=0, sticky="w", pady=5)
        access_key_entry = tk.Entry(frame, width=35, font=("Consolas", 10), show="*")
        access_key_entry.insert(0, self.asr_config.access_key)
        access_key_entry.grid(row=row, column=0, columnspan=2, pady=2, sticky="ew")
        row += 1

        # ── Separator ──
        tk.Frame(frame, bg="#ddd", height=1).grid(row=row, column=0, columnspan=2,
                                                    sticky="ew", pady=10)
        row += 1

        # Hotkey
        tk.Label(frame, text="快捷键:", bg="#f5f5f5", font=("Microsoft YaHei", 10),
                 anchor="w").grid(row=row, column=0, sticky="w", pady=5)
        hotkey_var = tk.StringVar(value=self.hotkey_name)
        hotkey_opts = ["f1", "f2", "f3", "f4", "f5", "f6",
                       "f7", "f8", "f9", "f10", "f11", "f12"]
        hotkey_menu = ttk.Combobox(frame, textvariable=hotkey_var, values=hotkey_opts,
                                    width=10, state="readonly", font=("Consolas", 10))
        hotkey_menu.grid(row=row, column=0, padx=(0, 10), sticky="w")
        row += 1

        # Separator
        tk.Frame(frame, bg="#ddd", height=1).grid(row=row, column=0, columnspan=2,
                                                    sticky="ew", pady=10)
        row += 1

        # ── Hint ──
        hint = tk.Text(frame, height=3, width=40, bg="#fef9e7", fg="#7f6000",
                       font=("Microsoft YaHei", 8), border=0, wrap="word")
        hint.insert("1.0",
                    "获取 API 凭证:\n"
                    "1. 打开 https://console.volcengine.com/doubao/voice\n"
                    "2. 创建应用 → 获取 APP ID 和 Access Token\n"
                    "3. 保存后即可使用 (Resource ID 已内置)")
        hint.config(state="disabled")
        hint.grid(row=row, column=0, columnspan=2, pady=5, sticky="ew")
        row += 1

        # ── Buttons ──
        btn_frame = tk.Frame(frame, bg="#f5f5f5")
        btn_frame.grid(row=row, column=0, columnspan=2, pady=10)

        def save_settings():
            self.asr_config.app_id = app_id_entry.get().strip()
            self.asr_config.access_key = access_key_entry.get().strip()
            new_hotkey = hotkey_var.get().strip().lower()

            if not self.asr_config.is_valid():
                messagebox.showwarning("提示", "请填写 APP ID 和 Access Token")
                return

            # Update hotkey if changed
            if new_hotkey != self.hotkey_name:
                self.hotkey_name = new_hotkey
                self._hotkey_var.set(HOTKEY_DISPLAY_NAMES.get(new_hotkey, new_hotkey.upper()))
                self._restart_hotkey_listener()
                # Update hint text
                for w in self.root.winfo_children():
                    for c in w.winfo_children():
                        if isinstance(c, tk.Label) and "按住" in c.cget("text"):
                            c.configure(
                                text=f"按住 {HOTKEY_DISPLAY_NAMES.get(new_hotkey, new_hotkey.upper())} 开始录音\n"
                                     f"松开自动识别并输入")

            self.asr_client = SeedASRClient(self.asr_config)
            self._save_config()
            dialog.destroy()
            messagebox.showinfo("设置已保存", "API 凭证已保存并生效")

        tk.Button(btn_frame, text="保存", bg="#3498db", fg="white",
                  font=("Microsoft YaHei", 9), padx=20, command=save_settings).pack(
            side="left", padx=5)
        tk.Button(btn_frame, text="取消", bg="#95a5a6", fg="white",
                  font=("Microsoft YaHei", 9), padx=20,
                  command=dialog.destroy).pack(side="left", padx=5)

    def _show_help(self):
        hotkey = HOTKEY_DISPLAY_NAMES.get(self.hotkey_name, self.hotkey_name.upper())
        msg = (
            f"🎤 语音输入法 v1.0\n\n"
            f"使用方法:\n"
            f"  1. 按住 {hotkey} → 开始录音\n"
            f"  2. 说话（持续按住）\n"
            f"  3. 松开 {hotkey} → 自动识别并输入\n\n"
            f"首次使用:\n"
            f"  点击「设置」填入 API 凭证\n"
            f"  凭证在火山引擎控制台获取\n\n"
            f"提示:\n"
            f"  - 程序启动后会在后台运行\n"
            f"  - 点击窗口关闭按钮可最小化到托盘"
        )
        messagebox.showinfo("帮助", msg)

    def _test_api(self):
        """Test the API connection with a short beep or generated tone."""
        if not self.asr_config.is_valid():
            messagebox.showwarning("提示", "请先在设置中填写 API 凭证")
            return

        self._set_state("transcribing")
        # Generate a short 1-second test tone (440Hz sine wave)
        t = np.linspace(0, 1.0, int(SAMPLE_RATE * 1.0), endpoint=False)
        test_audio = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).tobytes()

        def on_result(future):
            try:
                text = future.result()
                if text:
                    self.root.after(0, lambda: messagebox.showinfo("测试结果", f"识别结果: {text}"))
                else:
                    self.root.after(0, lambda: messagebox.showinfo("测试结果", "未返回识别结果（可能音频太短）"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("测试失败", str(e)))
            finally:
                self.root.after(0, lambda: self._set_state("idle"))

        self.async_engine.run(self.asr_client.transcribe(test_audio), on_result)

    # ── Hotkey listener ──

    def _start_hotkey_listener(self):
        """Start the pynput global hotkey listener in a thread."""
        try:
            from pynput import keyboard as kb
            hotkey = parse_hotkey(self.hotkey_name)

            def on_press(key):
                if key == hotkey and not self._pressed:
                    self._pressed = True
                    self._on_hotkey_down()

            def on_release(key):
                if key == hotkey and self._pressed:
                    self._pressed = False
                    self._on_hotkey_up()

            listener = kb.Listener(on_press=on_press, on_release=on_release)
            listener.daemon = True
            listener.start()
            self._pynput_listener = listener
            log.info(f"Hotkey listener started: {self.hotkey_name}")
        except Exception as e:
            log.error(f"Failed to start hotkey listener: {e}")

    def _restart_hotkey_listener(self):
        if self._pynput_listener:
            try:
                self._pynput_listener.stop()
            except Exception:
                pass
        self._pressed = False
        self._start_hotkey_listener()

    # ── Hotkey callbacks ──

    def _on_hotkey_down(self):
        """Called when hotkey is pressed - start recording."""
        if not self.asr_config.is_valid():
            log.warning("Credentials not configured")
            return
        if self.state == "transcribing" or self._processing:
            log.info("Still transcribing previous recording, ignoring hotkey press")
            return

        self.root.after(0, lambda: self._set_state("recording"))
        self.recorder.start()

    def _on_hotkey_up(self):
        """Called when hotkey is released - stop recording and transcribe."""
        if self.state != "recording" or self._processing:
            return
        self._processing = True

        pcm_data = self.recorder.stop()
        if pcm_data is None or len(pcm_data) < 320:  # <10ms of audio = probably accidental
            self._processing = False
            self.root.after(0, lambda: self._set_state("idle"))
            log.info("Audio too short, ignoring")
            return

        self.root.after(0, lambda: self._set_state("transcribing"))
        self._transcribe_and_type(pcm_data)

    def _transcribe_and_type(self, pcm_data: bytes):
        """Transcribe audio and insert the result into the focused window."""

        def on_result(future):
            self._processing = False
            try:
                text = future.result()
                if text:
                    log.info(f"Transcription result: {text}")
                    self._type_text(text)
                    self.root.after(0, lambda: self._set_state("idle"))
                else:
                    self.root.after(0, lambda: self._set_state("idle"))
            except Exception as e:
                log.error(f"Transcription failed: {e}")
                self._processing = False
                self.root.after(0, lambda: self._set_state("error"))
                self.root.after(2000, lambda: self._set_state("idle"))

        self.async_engine.run(self.asr_client.transcribe(pcm_data), on_result)

    def _type_text(self, text: str):
        """Type text into the currently focused window using clipboard paste."""
        try:
            old_clipboard = pyperclip.paste()
            pyperclip.copy(text)
            time.sleep(0.05)

            import keyboard as kb
            kb.press_and_release("ctrl+v")
            time.sleep(0.05)

            # Restore old clipboard after a short delay (give paste time to complete)
            def restore():
                time.sleep(0.3)
                pyperclip.copy(old_clipboard)

            threading.Thread(target=restore, daemon=True).start()
            log.info(f"Typed {len(text)} chars into focused window")
        except Exception as e:
            log.error(f"Failed to type text: {e}")

    # ── Window lifecycle ──

    def _on_closing(self):
        """Minimize to tray instead of closing."""
        if self._tray_icon:
            self.root.withdraw()
        else:
            self._try_create_tray_icon()

    def _try_create_tray_icon(self):
        """Try to create a system tray icon. If pystray is not available, quit."""
        try:
            from PIL import Image, ImageDraw
            import pystray

            # Create icon image
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([8, 8, 56, 56], fill="#3498db")
            draw.ellipse([20, 20, 44, 44], fill="white")
            draw.polygon([(26, 24), (26, 40), (40, 32)], fill="#3498db")

            def on_show(icon, item):
                icon.stop()
                self.root.after(0, self.root.deiconify)

            def on_quit(icon, item):
                icon.stop()
                self.root.after(0, self._really_quit)

            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", on_show, default=True),
                pystray.MenuItem("退出", on_quit),
            )
            self._tray_icon = pystray.Icon("voice_input", img, "语音输入法", menu)
            self.root.withdraw()
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
            log.info("System tray icon created")
        except ImportError:
            log.info("pystray not available, quitting on close")
            self._really_quit()

    def _really_quit(self):
        """Clean shutdown."""
        log.info("Shutting down...")
        if self._pynput_listener:
            try:
                self._pynput_listener.stop()
            except Exception:
                pass
        self.async_engine.shutdown()
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def run(self):
        """Run the application."""
        self.root.mainloop()


# ══════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════

def main():
    # Suppress websocket traffic logs
    logging.getLogger("websockets").setLevel(logging.WARNING)

    # Ensure asyncio policy for Windows
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    app = VoiceInputApp()
    app.run()


if __name__ == "__main__":
    main()
