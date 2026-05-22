"""
豆包 Seed ASR 语音输入法 (Streaming)
========================
按下快捷键开始录音，松手自动识别并输入文本

流式识别: 说话时实时显示识别结果在屏幕中央悬浮窗
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
import signal as _signal
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Callable

import numpy as np
import sounddevice as sd
import pyperclip

import ctypes
import ctypes.wintypes as wintypes

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
FRAME_DURATION_MS = 200  # 200ms per audio packet (optimal for streaming)

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

# Streaming endpoint (双向流式模式)
WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
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
#  Win32 floating overlay (no Tkinter)
# ══════════════════════════════════════════════

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# Set proper argument/return types for Win32 APIs to avoid overflow on x64
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_int64
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.UpdateWindow.restype = wintypes.BOOL
user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
user32.InvalidateRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetClientRect.restype = wintypes.BOOL
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.BeginPaint.restype = wintypes.HDC
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.EndPaint.restype = wintypes.BOOL
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_byte, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.DrawTextW.argtypes = [wintypes.HDC, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, wintypes.UINT]
user32.DrawTextW.restype = ctypes.c_int
user32.FillRect.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.HANDLE]
user32.FillRect.restype = ctypes.c_int
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.CreateWindowExW.argtypes = [wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                    wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int,
                                    wintypes.HWND, wintypes.HANDLE,
                                    wintypes.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.restype = ctypes.c_ushort

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.CreateSolidBrush.restype = wintypes.HANDLE
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wintypes.HANDLE
gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_wchar_p]
gdi32.CreateFontW.restype = wintypes.HANDLE
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
gdi32.SetTextColor.restype = wintypes.COLORREF
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetBkMode.restype = ctypes.c_int
gdi32.RoundRect.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int]
gdi32.RoundRect.restype = wintypes.BOOL

# Window styles
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOPMOST = 0x8
WS_EX_TOOLWINDOW = 0x80
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
SW_SHOW = 5
SW_HIDE = 0
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_DESTROY = 0x0002
TRANSPARENT = 1
FW_BOLD = 700
ANTIALIASED_QUALITY = 4
DEFAULT_CHARSET = 1
OUT_DEFAULT_PRECIS = 0
CLIP_DEFAULT_PRECIS = 0
PROOF_QUALITY = 2
FF_DONTCARE = 0
DEFAULT_PITCH = 0
SRCCOPY = 0x00CC0020
LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002
NULL_BRUSH = 5

# DrawText format constants
DT_CENTER = 0x00000001
DT_WORDBREAK = 0x00000010
DT_SINGLELINE = 0x00000020
DT_NOCLIP = 0x00000100
DT_CALCRECT = 0x00000400

# Chroma key color (RGB) — fuchsia will be transparent
CHROMA_KEY_RGB = 0x00FF00FF  # GDI: 0x00BBGGRR

# Overlay colors (BGR for GDI)
BG_BGR = 0x00222222  # dark background
TEXT_BGR = 0x00FFFFFF  # white

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long,
    wintypes.HWND, wintypes.UINT,
    wintypes.WPARAM, wintypes.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),      # HCURSOR = HANDLE
        ("hbrBackground", wintypes.HANDLE),  # HBRUSH = HANDLE
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", wintypes.HICON),
    ]


# Global registry of overlay instances for window proc dispatch
_overlay_registry: dict[int, 'FloatingOverlay'] = {}


def _overlay_wnd_proc(hwnd, msg, wparam, lparam):
    overlay = _overlay_registry.get(hwnd)
    if overlay:
        return overlay._handle_message(msg, wparam, lparam)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_wnd_proc_cb = WNDPROC(_overlay_wnd_proc)


class FloatingOverlay:
    """Screen-centered transparent text overlay using Win32 chroma-key.

    Appears as a floating overlay on top of all windows.
    Entirely transparent except for the rendered text area.
    Does NOT use Tkinter — pure Win32 API via ctypes.
    """

    def __init__(self):
        self.hwnd = None
        self._text = ""
        self._visible = False
        self._setup_window()

    def _setup_window(self):
        instance = kernel32.GetModuleHandleW(None)
        class_name = "VoiceInputOverlay_v2"

        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.style = 0
        wc.lpfnWndProc = _wnd_proc_cb
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = instance
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = class_name
        wc.hIconSm = None

        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if atom == 0:
            # Class may already be registered
            pass

        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)

        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
            | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            class_name, "",
            WS_POPUP,
            0, 0, screen_w, screen_h,
            None, None, instance, None
        )

        if not self.hwnd:
            log.error(f"Failed to create overlay window: {ctypes.GetLastError()}")
            return

        # Chroma key: fuchsia (B,G,R) = (255, 0, 255) → transparent
        # Global alpha: 230/255 (90% opacity for non-keyed areas)
        user32.SetLayeredWindowAttributes(self.hwnd, CHROMA_KEY_RGB, 230,
                                          LWA_COLORKEY | LWA_ALPHA)

        _overlay_registry[self.hwnd] = self

    def _handle_message(self, msg, wparam, lparam):
        if msg == WM_PAINT:
            self._on_paint()
            return 0
        elif msg == WM_ERASEBKGND:
            return 1
        elif msg == WM_DESTROY:
            _overlay_registry.pop(self.hwnd, None)
            return 0
        return user32.DefWindowProcW(self.hwnd, msg, wparam, lparam)

    def _on_paint(self):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
        if not hdc:
            return

        try:
            rect = RECT()
            user32.GetClientRect(self.hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top

            # Double buffer to avoid flicker
            hdc_mem = gdi32.CreateCompatibleDC(hdc)
            hbitmap = gdi32.CreateCompatibleBitmap(hdc, w, h)
            old_bitmap = gdi32.SelectObject(hdc_mem, hbitmap)

            # Fill entire surface with chroma key (transparent)
            chroma_brush = gdi32.CreateSolidBrush(CHROMA_KEY_RGB)
            user32.FillRect(hdc_mem, ctypes.byref(rect), chroma_brush)
            gdi32.DeleteObject(chroma_brush)

            if self._text:
                self._draw_text(hdc_mem, w, h)

            # Blit to window
            gdi32.BitBlt(hdc, 0, 0, w, h, hdc_mem, 0, 0, SRCCOPY)

            gdi32.SelectObject(hdc_mem, old_bitmap)
            gdi32.DeleteObject(hbitmap)
            gdi32.DeleteDC(hdc_mem)
        finally:
            user32.EndPaint(self.hwnd, ctypes.byref(ps))

    def _draw_text(self, hdc, screen_w, screen_h):
        """Render centered text with dark background bubble."""
        name = "Microsoft YaHei"
        font_size = 40

        hfont = gdi32.CreateFontW(
            font_size, 0, 0, 0, FW_BOLD, False, False, False,
            DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
            ANTIALIASED_QUALITY, DEFAULT_PITCH | FF_DONTCARE, name
        )
        if not hfont:
            return
        old_font = gdi32.SelectObject(hdc, hfont)

        # Measure text with word wrap
        max_text_w = min(screen_w - 160, 1200)
        text_rect = RECT(0, 0, max_text_w, 0)
        user32.DrawTextW(hdc, self._text, -1, ctypes.byref(text_rect),
                         DT_CALCRECT | DT_WORDBREAK)

        text_w = text_rect.right - text_rect.left
        text_h = text_rect.bottom - text_rect.top

        margin_x = 50
        margin_y = 30
        bg_w = text_w + margin_x * 2
        bg_h = text_h + margin_y * 2 + 10

        # Center on screen
        bg_left = (screen_w - bg_w) // 2
        bg_top = (screen_h - bg_h) // 2
        bg_right = bg_left + bg_w
        bg_bottom = bg_top + bg_h

        # Draw rounded rect background
        bg_brush = gdi32.CreateSolidBrush(BG_BGR)
        null_pen = gdi32.GetStockObject(NULL_BRUSH)
        old_pen = gdi32.SelectObject(hdc, null_pen)
        old_brush = gdi32.SelectObject(hdc, bg_brush)

        radius = 20
        gdi32.RoundRect(hdc, bg_left, bg_top, bg_right, bg_bottom, radius * 2, radius * 2)

        gdi32.SelectObject(hdc, old_brush)
        gdi32.DeleteObject(bg_brush)
        gdi32.SelectObject(hdc, old_pen)

        # Draw text centered in background
        gdi32.SetTextColor(hdc, TEXT_BGR)
        gdi32.SetBkMode(hdc, TRANSPARENT)

        text_rect.left = bg_left + margin_x
        text_rect.top = bg_top + margin_y + 5
        text_rect.right = bg_right - margin_x
        text_rect.bottom = bg_bottom - margin_y - 5

        user32.DrawTextW(hdc, self._text, -1, ctypes.byref(text_rect),
                         DT_WORDBREAK | DT_CENTER | DT_NOCLIP)

        gdi32.SelectObject(hdc, old_font)
        gdi32.DeleteObject(hfont)

    def show(self, text=""):
        """Show the overlay with optional initial text."""
        if not self.hwnd:
            return
        self._text = text
        user32.ShowWindow(self.hwnd, SW_SHOW)
        self._redraw()
        self._visible = True

    def update(self, text: str):
        """Update displayed text on the overlay."""
        if not self.hwnd or not self._visible:
            return
        self._text = text
        self._redraw()

    def hide(self):
        """Hide the overlay."""
        if not self.hwnd:
            return
        self._visible = False
        user32.ShowWindow(self.hwnd, SW_HIDE)

    def _redraw(self):
        user32.InvalidateRect(self.hwnd, None, True)
        user32.UpdateWindow(self.hwnd)

    def destroy(self):
        if self.hwnd:
            _overlay_registry.pop(self.hwnd, None)
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None


# ══════════════════════════════════════════════
#  Protocol helpers
# ══════════════════════════════════════════════

def make_header(msg_type: int, flags: int = FLAG_POS_SEQUENCE,
                serialization: int = SERIALIZATION_JSON,
                compression: int = COMPRESSION_GZIP) -> bytes:
    return struct.pack(
        "!BBBB",
        (0b0001 << 4) | 0b0001,
        (msg_type << 4) | flags,
        (serialization << 4) | compression,
        0x00,
    )


def pack_full_client_request(params: dict, seq: int = 1) -> bytes:
    payload = gzip.compress(json.dumps(params, ensure_ascii=False).encode("utf-8"))
    header = make_header(FULL_CLIENT_REQUEST, FLAG_POS_SEQUENCE,
                         SERIALIZATION_JSON, COMPRESSION_GZIP)
    return header + struct.pack(">i", seq) + struct.pack(">I", len(payload)) + payload


def pack_audio_chunk(pcm_data: bytes, seq: int, is_last: bool = False) -> bytes:
    payload = gzip.compress(pcm_data)
    flags = FLAG_NEG_SEQUENCE if is_last else FLAG_POS_SEQUENCE
    header = make_header(AUDIO_ONLY_REQUEST, flags,
                         SERIALIZATION_JSON, COMPRESSION_GZIP)
    seq_val = -seq if is_last else seq
    return header + struct.pack(">i", seq_val) + struct.pack(">I", len(payload)) + payload


def parse_response(data: bytes) -> tuple[Optional[dict], bool]:
    if len(data) < 4:
        return None, True

    header = data[:4]
    msg_type = (header[1] >> 4) & 0x0f
    flags = header[1] & 0x0f
    compression = header[2] & 0x0f

    offset = 4
    if flags & 0x01:
        offset += 4
    if flags & 0x04:
        offset += 4

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
        start = raw.find(b'{')
        if start > 0:
            raw = raw[start:]
        result = json.loads(raw.decode("utf-8"))
        return result, bool(flags & 0x02)
    except Exception as e:
        log.error(f"Parse error: {e}")
        return None, True


# ══════════════════════════════════════════════
#  Audio recorder with streaming callback support
# ══════════════════════════════════════════════

class AudioRecorder:
    """Records PCM audio from microphone.

    Supports two modes:
    - Streaming: provide chunk_callback, called with each chunk
    - Buffered: no callback, use stop() to get all audio
    """

    def __init__(self):
        self._buffer: list[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._recording = False
        self._lock = threading.Lock()
        self._chunk_callback: Optional[Callable[[bytes], None]] = None
        self._total_bytes = 0

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self, chunk_callback: Optional[Callable[[bytes], None]] = None) -> None:
        """Start recording. If chunk_callback is set, called with each PCM chunk."""
        self._chunk_callback = chunk_callback
        self._total_bytes = 0
        with self._lock:
            self._buffer.clear()
            self._recording = True

        def callback(indata, frames, time_info, status):
            if status:
                log.warning(f"Audio callback status: {status}")
            if not self._recording:
                return
            data = indata.copy()
            if self._chunk_callback:
                self._total_bytes += len(data)
                self._chunk_callback(data.tobytes())
            else:
                with self._lock:
                    self._buffer.append(data)

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=int(SAMPLE_RATE * FRAME_DURATION_MS / 1000),
            callback=callback,
        )
        self._stream.start()
        log.info("Recording started")

    def stop(self) -> tuple[Optional[bytes], int]:
        """Stop recording.

        Returns:
            (pcm_bytes_or_None, total_bytes_recorded)
            In streaming mode, pcm_bytes is None (audio already streamed via callback).
            In buffered mode, pcm_bytes is the complete audio.
        """
        self._recording = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        total = self._total_bytes

        if self._chunk_callback:
            log.info(f"Recording stopped: {total} bytes streamed ({total / (SAMPLE_RATE * 2):.1f}s)")
            return None, total

        with self._lock:
            if not self._buffer:
                log.info("No audio recorded")
                return None, 0
            audio = np.concatenate(self._buffer, axis=0).tobytes()
            duration = len(audio) / (SAMPLE_RATE * 2)
            log.info(f"Recording stopped: {len(self._buffer)} chunks, "
                     f"{len(audio)} bytes ({duration:.1f}s)")
            return audio, len(audio)


# ══════════════════════════════════════════════
#  ASR Config
# ══════════════════════════════════════════════

@dataclass
class ASRConfig:
    app_id: str = ""
    access_key: str = ""

    def is_valid(self) -> bool:
        return bool(self.app_id and self.access_key)


# ══════════════════════════════════════════════
#  Streaming Seed ASR WebSocket client
# ══════════════════════════════════════════════

class StreamASRClient:
    """Streaming WebSocket client for Doubao Seed ASR.

    Sends audio chunks as they arrive via push_audio() (thread-safe).
    Returns partial results via on_partial and final result via on_final.
    """

    def __init__(self, config: ASRConfig,
                 on_partial: Optional[Callable[[str], None]] = None,
                 on_final: Optional[Callable[[str], None]] = None):
        self.config = config
        self.on_partial = on_partial
        self.on_final = on_final
        self._audio_queue: queue.Queue = queue.Queue()
        self._last_text = ""
        self._done = threading.Event()

    def push_audio(self, chunk: bytes, is_last: bool = False):
        """Push an audio chunk for transmission. Thread-safe."""
        self._audio_queue.put((chunk, is_last))
        if is_last:
            log.info("Last audio chunk enqueued")

    async def start(self) -> str:
        """Connect to the streaming endpoint and run the session.

        Returns the final transcribed text.
        """
        if not self.config.is_valid():
            raise ValueError("API credentials not configured")

        req_id = str(uuid.uuid4())
        headers = {
            "X-Api-App-Key": self.config.app_id,
            "X-Api-Access-Key": self.config.access_key,
            "X-Api-Resource-Id": RESOURCE_ID,
            "X-Api-Connect-Id": req_id,
        }

        log.info("Connecting to Seed ASR (streaming)...")
        import websockets
        from websockets.exceptions import InvalidStatus

        try:
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

                # Read welcome response
                resp = await ws.recv()
                if isinstance(resp, bytes):
                    body, _ = parse_response(resp)
                    if body:
                        log.debug(f"Server welcome received")

                # ── 2. Concurrent send/recv ──
                send_task = asyncio.create_task(self._send_loop(ws))
                recv_task = asyncio.create_task(self._recv_loop(ws))

                # Wait for recv to complete (final result received)
                result = await recv_task
                send_task.cancel()
                try:
                    await send_task
                except (asyncio.CancelledError, Exception):
                    pass

                self._last_text = result
                self._done.set()
                log.info(f"Streaming ASR complete: {result[:100] if result else '(empty)'}")
                return result

        except InvalidStatus as e:
            log.error(f"HTTP {e.response.status_code}: {e.response.reason_phrase}")
            try:
                body = getattr(e.response, 'body', None)
                if body:
                    log.error(f"Response body: {body.decode('utf-8', errors='replace')[:500]}")
            except Exception:
                pass
            try:
                log.error(f"Response headers: {dict(e.response.headers)}")
            except Exception:
                pass
            self._done.set()
            raise
        except Exception as e:
            log.error(f"Streaming ASR failed: {e}")
            self._done.set()
            raise

    async def _send_loop(self, ws):
        """Read from audio queue and send via WebSocket."""
        seq = 2
        while True:
            # Run queue.get in executor to avoid blocking event loop
            item = await asyncio.get_event_loop().run_in_executor(
                None, self._audio_queue.get
            )
            chunk, is_last = item
            if chunk or is_last:  # always send last packet (even empty) to signal EOF
                await ws.send(pack_audio_chunk(chunk, seq, is_last))
                seq += 1
            if is_last:
                break

    async def _recv_loop(self, ws) -> str:
        """Receive results from WebSocket until final."""
        full_text = ""
        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                log.warning("Receive timeout, using last result")
                break
            except Exception:
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
                    log.debug(f"Partial: {text[:60]}")
                    if not is_final and self.on_partial:
                        self.on_partial(text)
                    if is_final:
                        log.info(f"Final result: {text[:100]}")
                        if self.on_final:
                            self.on_final(text)
                        return full_text
        if self.on_final and full_text:
            self.on_final(full_text)
        return full_text

    def wait_result(self, timeout: float = 30.0) -> str:
        """Block until the streaming session completes. Thread-safe."""
        self._done.wait(timeout=timeout)
        return self._last_text


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
        self.async_engine = AsyncEngine()

        # Streaming ASR session (created per-recording)
        self._stream_asr: Optional[StreamASRClient] = None

        # Floating overlay — pure Win32, no Tkinter
        self.overlay = FloatingOverlay()

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
        log.info("Voice Input App started (streaming mode)")

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
        self._hotkey_var = tk.StringVar(
            value=HOTKEY_DISPLAY_NAMES.get(self.hotkey_name, self.hotkey_name.upper()))
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
            colors = {"idle": "#2ecc71", "recording": "#e74c3c",
                      "transcribing": "#f39c12", "error": "#e74c3c"}
            color = colors.get(state, "#333")
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
        if self._pynput_listener:
            try:
                self._pynput_listener.stop()
            except Exception:
                pass

        captured = [None]

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
            return False

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
                            c.configure(
                                text=f"按住 {HOTKEY_DISPLAY_NAMES.get(captured[0], captured[0].upper())} 开始录音\n"
                                     f"松开自动识别并输入")
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
        dialog.geometry("460x420")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg="#f5f5f5")

        frame = tk.Frame(dialog, bg="#f5f5f5", padx=20, pady=15)
        frame.pack(fill="both", expand=True)

        row = 0

        # ── Section: API 凭证 ──
        api_header = tk.Label(frame, text="🔑 API 凭证", font=("Microsoft YaHei", 11, "bold"),
                              bg="#f5f5f5", fg="#2c3e50", anchor="w")
        api_header.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        row += 1

        # APP ID (row with label above)
        tk.Label(frame, text="APP ID（应用ID）", bg="#f5f5f5",
                 font=("Microsoft YaHei", 9), fg="#555", anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        app_id_entry = tk.Entry(frame, width=40, font=("Consolas", 10))
        app_id_entry.insert(0, self.asr_config.app_id)
        app_id_entry.grid(row=row, column=0, columnspan=2, pady=(0, 8), sticky="ew")
        row += 1

        # Access Token
        tk.Label(frame, text="Access Token（访问令牌）", bg="#f5f5f5",
                 font=("Microsoft YaHei", 9), fg="#555", anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        access_key_entry = tk.Entry(frame, width=40, font=("Consolas", 10))
        access_key_entry.insert(0, self.asr_config.access_key)
        access_key_entry.grid(row=row, column=0, columnspan=2, pady=(0, 5), sticky="ew")
        row += 1

        # Hint: source
        source_hint = tk.Label(frame,
                               text="从火山引擎控制台 → 豆包语音 → 流式语音识别模型2.0 获取",
                               font=("Microsoft YaHei", 8), bg="#f5f5f5", fg="#999",
                               anchor="w", justify="left")
        source_hint.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 5))
        row += 1

        # Separator
        tk.Frame(frame, bg="#ddd", height=1).grid(row=row, column=0, columnspan=2,
                                                    sticky="ew", pady=8)
        row += 1

        # ── Section: 快捷键 ──
        hotkey_header = tk.Label(frame, text="⌨️ 快捷键", font=("Microsoft YaHei", 11, "bold"),
                                 bg="#f5f5f5", fg="#2c3e50", anchor="w")
        hotkey_header.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        row += 1

        hotkey_var = tk.StringVar(value=self.hotkey_name)
        hotkey_opts = ["f1", "f2", "f3", "f4", "f5", "f6",
                       "f7", "f8", "f9", "f10", "f11", "f12"]
        tk.Label(frame, text="录音快捷键:", bg="#f5f5f5",
                 font=("Microsoft YaHei", 9), fg="#555").grid(
            row=row, column=0, sticky="w")
        hotkey_menu = ttk.Combobox(frame, textvariable=hotkey_var, values=hotkey_opts,
                                    width=8, state="readonly", font=("Consolas", 10))
        hotkey_menu.grid(row=row, column=0, padx=(100, 0), sticky="w")
        row += 1

        # Separator
        tk.Frame(frame, bg="#ddd", height=1).grid(row=row, column=0, columnspan=2,
                                                    sticky="ew", pady=8)
        row += 1

        # Buttons
        btn_frame = tk.Frame(frame, bg="#f5f5f5")
        btn_frame.grid(row=row, column=0, columnspan=2, pady=(5, 10))

        def save_settings():
            self.asr_config.app_id = app_id_entry.get().strip()
            self.asr_config.access_key = access_key_entry.get().strip()
            new_hotkey = hotkey_var.get().strip().lower()

            if not self.asr_config.is_valid():
                messagebox.showwarning("提示", "请填写 APP ID 和 Access Token")
                return

            if new_hotkey != self.hotkey_name:
                self.hotkey_name = new_hotkey
                self._hotkey_var.set(HOTKEY_DISPLAY_NAMES.get(new_hotkey, new_hotkey.upper()))
                self._restart_hotkey_listener()
                for w in self.root.winfo_children():
                    for c in w.winfo_children():
                        if isinstance(c, tk.Label) and "按住" in c.cget("text"):
                            c.configure(
                                text=f"按住 {HOTKEY_DISPLAY_NAMES.get(new_hotkey, new_hotkey.upper())} 开始录音\n"
                                     f"松开自动识别并输入")

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
            f"🎤 语音输入法 v2.0 (流式)\n\n"
            f"使用方法:\n"
            f"  1. 按住 {hotkey} → 开始录音，屏幕中央显示实时识别\n"
            f"  2. 说话（持续按住），识别结果实时更新\n"
            f"  3. 松开 {hotkey} → 自动输入文本\n\n"
            f"首次使用:\n"
            f"  点击「设置」填入 API 凭证\n"
            f"  凭证在火山引擎控制台获取\n\n"
            f"提示:\n"
            f"  - 程序启动后会在后台运行\n"
            f"  - 点击窗口关闭按钮可最小化到托盘"
        )
        messagebox.showinfo("帮助", msg)

    def _test_api(self):
        if not self.asr_config.is_valid():
            messagebox.showwarning("提示", "请先在设置中填写 API 凭证")
            return

        self._set_state("transcribing")
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

        # Use the non-streaming test via StreamASRClient for simplicity
        async def test_transcribe():
            client = StreamASRClient(self.asr_config)
            # Push audio and signal end
            client.push_audio(test_audio, is_last=True)
            return await client.start()

        self.async_engine.run(test_transcribe(), on_result)

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
        """Hotkey pressed — show overlay, start recording + streaming ASR."""
        if not self.asr_config.is_valid():
            log.warning("Credentials not configured")
            return
        if self.state == "transcribing" or self._processing:
            log.info("Still processing previous recording, ignoring")
            return

        self.root.after(0, lambda: self._set_state("recording"))

        # Show floating overlay (Win32, not Tkinter)
        self.overlay.show("...")

        # Create streaming ASR client with partial result callback
        self._stream_asr = StreamASRClient(
            self.asr_config,
            on_partial=lambda text: self.root.after(0, lambda: self.overlay.update(text)),
        )

        # Start streaming ASR session in background
        # Use done callback for completion (cleaner than on_final race)
        future = self.async_engine.run(self._stream_asr.start())
        future.add_done_callback(
            lambda f: self.root.after(0, lambda: self._on_stream_session_end(f))
        )

        # Start recording with streaming callback → feeds audio to ASR
        self.recorder.start(chunk_callback=self._on_audio_chunk)

    def _on_audio_chunk(self, chunk_bytes: bytes):
        """Called from audio callback (system thread) for each 200ms chunk."""
        if self._stream_asr:
            self._stream_asr.push_audio(chunk_bytes, is_last=False)

    def _on_hotkey_up(self):
        """Hotkey released — stop recording, send last chunk, wait for final."""
        if self.state != "recording" or self._processing:
            return
        self._processing = True

        # Stop recording (audio already streamed via callback)
        _, total_bytes = self.recorder.stop()

        if total_bytes < 320:  # <10ms = accidental press
            self._processing = False
            self.overlay.hide()
            self._stream_asr = None
            self.root.after(0, lambda: self._set_state("idle"))
            log.info("Audio too short, ignoring")
            return

        # Signal last audio chunk — ASR will finalize
        if self._stream_asr:
            self._stream_asr.push_audio(b"", is_last=True)

        self.root.after(0, lambda: self._set_state("transcribing"))

    def _on_stream_session_end(self, future):
        """Called on main thread (via root.after) when streaming session finishes."""
        try:
            text = future.result()  # may raise on error
        except Exception as e:
            log.error(f"ASR streaming failed: {e}")
            self.overlay.hide()
            self._processing = False
            self._stream_asr = None
            self._set_state("error")
            self.root.after(2000, lambda: self._set_state("idle"))
            return

        self.overlay.hide()

        if text:
            log.info(f"Transcription result: {text}")
            self._type_text(text)

        self._processing = False
        self._stream_asr = None
        self._set_state("idle")

    def _type_text(self, text: str):
        """Type text into the currently focused window using clipboard paste."""
        try:
            old_clipboard = pyperclip.paste()
            pyperclip.copy(text)
            time.sleep(0.05)

            import keyboard as kb
            kb.press_and_release("ctrl+v")
            time.sleep(0.05)

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
        self.overlay.destroy()
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
        _signal.signal(_signal.SIGINT, lambda sig, frame: self.root.after(0, self._really_quit))
        self.root.mainloop()


# ══════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════

def main():
    logging.getLogger("websockets").setLevel(logging.WARNING)

    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    app = VoiceInputApp()
    app.run()


if __name__ == "__main__":
    main()
