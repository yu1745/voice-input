#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
voice_input.py — 全局快捷键语音输入（qwen3-asr-flash 批量识别）

操作（按住说话 / 松开输出）：
  · 按住 Ctrl + Win(Super)  →  开始录音（底部弹出半透明窗）
                              录音期间流式模型(fun-asr)实时出字预览
  · 松开 Ctrl + Win         →  停止录音，整段送 qwen3-asr-flash 批量识别（全局优化），
                              最终结果粘贴到当前光标
  · 状态错乱/卡住时再按一次  →  重置当前会话并重新开始（而不是忽略）

双模型分工：
  实时预览  fun-asr-realtime（WebSocket 流式）边录边出字，仅供你看着反馈
  最终结果  qwen3-asr-flash（批量）看整段音频全局优化，这个才是粘贴的

技术：
  热键   python-xlib QueryKeymap 轮询（无需 root）
  录音   sounddevice 16kHz/单声道/int16，软件增益(GAIN)
  识别   批量: qwen3-asr-flash OpenAI兼容 base64直传（最终结果）
         预览: fun-asr-realtime WebSocket 流式（实时反馈）
  输出   剪贴板 + xdotool Ctrl+V（CJK 最可靠），2 秒后恢复原剪贴板
  界面   PyQt5 无边框半透明置顶窗口
"""
import os, sys, json, time, base64, io, wave, uuid, threading, subprocess, signal
import queue

CFG = json.load(open(os.path.expanduser("~/.config/voicetype/config.json")))
API_KEY = CFG["api_key"]
ASR_URL = CFG.get("asr_url", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
MODEL = CFG.get("model", "qwen3-asr-flash")
STREAM_WS = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"
STREAM_MODEL = "fun-asr-realtime-2026-02-28"
LANG = CFG.get("language", "zh")
GAIN = float(CFG.get("gain", 4.0))
SR = 16000
CHUNK = 3200                      # 200ms 帧（边录音边推流）

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import pyqtSignal, QObject, Qt
import numpy as np
import sounddevice as sd
import requests
import websocket
from websocket import ABNF
from Xlib import display as xlib_display

XLIB = xlib_display.Display()

def _keycodes():
    ctrl = [XLIB.keysym_to_keycode(s) for s in (0xffe3, 0xffe4)]
    sup = [XLIB.keysym_to_keycode(s) for s in (0xffeb, 0xffec)]
    return [k for k in ctrl if k], [k for k in sup if k]

CTRL_KC, SUPER_KC = _keycodes()
def _pressed(km, kcs): return any(km[k // 8] & (1 << (k % 8)) for k in kcs)


# ───────────────────────── 半透明悬浮窗 ─────────────────────────
class Overlay(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.frame = QtWidgets.QFrame(self)
        self.frame.setStyleSheet("QFrame{background:rgba(16,16,22,235); border-radius:20px;}")
        h = QtWidgets.QHBoxLayout(self.frame); h.setContentsMargins(24, 16, 24, 16)
        v = QtWidgets.QVBoxLayout(); v.setSpacing(1)
        self.dot = QtWidgets.QLabel("●"); self.dot.setStyleSheet("color:#ff4d4f; font-size:22px;")
        self.hint = QtWidgets.QLabel("录音中"); self.hint.setStyleSheet("color:#8aa0b6; font-size:12px;")
        v.addWidget(self.dot, alignment=Qt.AlignCenter)
        v.addWidget(self.hint, alignment=Qt.AlignCenter)
        h.addLayout(v); h.addSpacing(16)
        self.text = QtWidgets.QLabel("等待说话…")
        self.text.setStyleSheet("color:#f5f7fa; font-size:22px; font-weight:500;")
        self.text.setWordWrap(True)
        h.addWidget(self.text, stretch=1)
        outer = QtWidgets.QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(self.frame)
        self._on = False
        self.timer = QtCore.QTimer(self); self.timer.timeout.connect(self._blink)
        s = QtWidgets.QApplication.primaryScreen().size()
        self.setFixedWidth(min(940, int(s.width() * 0.6))); self.adjustSize()

    def _blink(self):
        self._on = not self._on
        self.dot.setStyleSheet("color:#ff4d4f; font-size:22px;" if self._on else "color:#5a1d1f; font-size:22px;")

    def set_text(self, t):  self.text.setText(t if t else "等待说话…")
    def set_status(self, s, color="#8aa0b6"): self.hint.setText(s); self.hint.setStyleSheet(f"color:{color}; font-size:12px;")

    def show_at(self, status):
        self.set_text(""); self.set_status(status)
        self.timer.start(520); self.show()
        s = QtWidgets.QApplication.primaryScreen().size()
        self.move((s.width() - self.width()) // 2, s.height() - self.height() - 90)

    def hide_done(self): self.timer.stop(); self.hide()


# ───────────────────────── 录音器（边录边喂流式预览）─────────────────────────
class Recorder:
    def __init__(self, on_frame=None):
        self.on_frame = on_frame        # 每帧回调（可选，用于实时推流）
        self.chunks = []
        self.stream = None

    def start(self):
        self.stream = sd.InputStream(samplerate=SR, channels=1, dtype="int16",
                                     blocksize=CHUNK, callback=self._cb)
        self.stream.start()

    def _cb(self, indata, *_):
        frame = indata.copy()
        self.chunks.append(frame)
        if self.on_frame:
            # 应用增益后推送给流式识别
            y = frame.astype(np.float32)
            if GAIN != 1.0:
                y = (y * GAIN).clip(-32768, 32767)
            self.on_frame(y.astype(np.int16).tobytes())

    def stop(self):
        try: self.stream.stop(); self.stream.close()
        except Exception: pass
        if not self.chunks: return b""
        audio = np.concatenate(self.chunks, axis=0).astype(np.float32)
        if GAIN != 1.0:
            audio = (audio * GAIN).clip(-32768, 32767)
        pcm = audio.astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
        return buf.getvalue()


# ───────────────────────── 批量 ASR（qwen3-asr-flash，流式输出）─────────────────────────
def transcribe(wav_bytes, on_text):
    data_uri = "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode()
    body = {"model": MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": data_uri}}]}],
            "stream": True, "asr_options": {"enable_itn": False}}
    if LANG: body["asr_options"]["language"] = LANG
    full = ""
    r = requests.post(ASR_URL, timeout=60, stream=True,
                      headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                      json=body)
    r.raise_for_status()
    for line in r.iter_lines():
        if not line: continue
        s = line.decode("utf-8", "ignore")
        if not s.startswith("data: "): continue
        data = s[6:]
        if data.strip() == "[DONE]": break
        try: ch = json.loads(data)
        except Exception: continue
        cs = ch.get("choices") or []
        if cs:
            d = cs[0].get("delta", {}).get("content", "")
            if d: full += d; on_text(full)
    return full


# ───────────────────────── 流式实时预览（边录边送 fun-asr-realtime）─────────────────────────
class StreamPreview:
    """录音期间开 WebSocket，边推音频边把识别结果回调出来（供实时显示）。"""
    def __init__(self, on_partial, on_error):
        self.on_partial = on_partial
        self.on_error = on_error
        self.task_id = uuid.uuid4().hex[:32]
        self.started = threading.Event()
        self.stopping = False
        self.q = queue.Queue()
        self.sentences = {}            # sentence_id -> 最终文本
        self.cur_partial = ""
        self.ws = None

    def start(self):
        self.ws = websocket.WebSocketApp(
            STREAM_WS, header={"Authorization": f"bearer {API_KEY}"},
            on_open=self._on_open, on_message=self._on_msg,
            on_error=self._on_err, on_close=lambda *a: None)
        threading.Thread(target=self.ws.run_forever, daemon=True).start()
        threading.Thread(target=self._sender, daemon=True).start()

    def feed(self, pcm_bytes):
        """录音回调里调用：把一帧音频同时入队推流。"""
        if not self.stopping:
            self.q.put(pcm_bytes)

    def _on_open(self, ws):
        ws.send(json.dumps({"header": {"action": "run-task", "task_id": self.task_id, "streaming": "duplex"},
                            "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                                        "model": STREAM_MODEL,
                                        "parameters": {"sample_rate": SR, "format": "pcm"}, "input": {}}}))

    def _joined(self):
        return "".join(self.sentences[k] for k in sorted(self.sentences))

    def _on_msg(self, ws, data):
        try: m = json.loads(data)
        except Exception: return
        ev = m.get("header", {}).get("event", "")
        if ev == "task-started":
            self.started.set()
        elif ev == "result-generated":
            s = m.get("payload", {}).get("output", {}).get("sentence", {})
            t, sid, end = s.get("text", ""), s.get("sentence_id"), s.get("sentence_end", False)
            if end and sid is not None:
                self.sentences[sid] = t; self.cur_partial = ""
                self.on_partial(self._joined())
            elif t:
                self.cur_partial = t
                self.on_partial(self._joined() + t)
        elif ev == "task-failed":
            self.on_error(m.get("header", {}).get("error_message", "stream task-failed"))

    def _on_err(self, ws, e):
        self.on_error("stream WS: " + str(e))

    def _sender(self):
        self.started.wait(timeout=10)
        while not self.stopping:
            try: chunk = self.q.get(timeout=0.1)
            except queue.Empty: continue
            try: self.ws.send(chunk, opcode=ABNF.OPCODE_BINARY)
            except Exception: break
        # 排空剩余并 finish-task
        while True:
            try: chunk = self.q.get_nowait()
            except queue.Empty: break
            try: self.ws.send(chunk, opcode=ABNF.OPCODE_BINARY)
            except Exception: break
        try:
            self.ws.send(json.dumps({"header": {"action": "finish-task", "task_id": self.task_id,
                                                "streaming": "duplex"}, "payload": {"input": {}}}))
        except Exception: pass

    def stop(self):
        self.stopping = True


# ───────────────────────── 控制器（主线程）─────────────────────────
class Controller(QObject):
    sig_press = pyqtSignal(); sig_release = pyqtSignal()
    sig_show = pyqtSignal(str)
    sig_update = pyqtSignal(str)
    sig_hide = pyqtSignal()
    sig_done = pyqtSignal(str, int)      # (text, gen)  gen=会话代号，作废过期结果用
    sig_error = pyqtSignal(str, int)     # (msg, gen)

    def __init__(self, overlay):
        super().__init__()
        self.overlay = overlay; self.state = "idle"; self.rec = None; self.preview = None
        self.gen = 0                      # 会话代号：每次重置 +1，隔离在途的旧批量识别
        self.sig_press.connect(self._do_start)
        self.sig_release.connect(self._do_stop)
        self.sig_show.connect(lambda s: self.overlay.show_at(s))
        self.sig_update.connect(self.overlay.set_text)
        self.sig_hide.connect(self.overlay.hide_done)
        self.sig_done.connect(self.on_done)
        self.sig_error.connect(self.on_error)

    def press(self):   self.sig_press.emit()      # 热键线程
    def release(self): self.sig_release.emit()

    def _abort_current(self):
        """丢弃当前会话（录音 / 实时预览 / 在途批量识别），回到 idle。"""
        if self.preview:
            self.preview.stop()
            try:
                if self.preview.ws:
                    self.preview.ws.close()
            except Exception:
                pass
            self.preview = None
        if self.rec:
            try:
                self.rec.stop()
            except Exception:
                pass
            self.rec = None
        self.gen += 1                 # 在途的批量识别结果一律作废，不再落地
        self.state = "idle"
        self.sig_hide.emit()

    def _do_start(self):
        if self.state != "idle":
            # 状态错乱（松开事件丢失 / 批量识别挂起）时再次按下：
            # 重置当前会话重新开始，而不是忽略——保证快捷键永远能恢复
            print(f"[语音输入] ↺ 按下时状态异常({self.state})，重置会话后重新开始", flush=True)
            self._abort_current()
        self.state = "recording"
        # 开流式预览（边录边出字）
        self.preview = StreamPreview(
            on_partial=lambda t: self.sig_update.emit(t),
            on_error=lambda m: print("[语音输入] (预览) ⚠️", m, flush=True))
        self.preview.start()
        self.rec = Recorder(on_frame=self.preview.feed)
        self.rec.start()
        self.sig_show.emit("录音中")
        print("[语音输入] ▶ 录音中（边录边预览，松开 Ctrl+Win 结束）", flush=True)

    def _do_stop(self):
        if self.state != "recording": return
        # 1) 先停预览流（不再推流，但不阻塞界面）
        if self.preview: self.preview.stop()
        # 2) 拿到完整音频
        wav = self.rec.stop(); self.rec = None
        dur = max(0, len(wav) - 44) / 2 / SR       # 粗略秒数
        print(f"[语音输入] ⏹ 录音结束 {dur:.1f}s，批量识别中…", flush=True)
        if dur < 0.3:
            self.state = "idle"; self.preview = None; self.sig_hide.emit(); return
        self.state = "transcribing"
        self.sig_show.emit("识别中")
        gen = self.gen
        threading.Thread(target=self._asr_thread, args=(wav, gen), daemon=True).start()

    def _asr_thread(self, wav, gen):
        try:
            # 批量模型（全局优化）——这才是最终采信的结果
            def _on_partial(t):
                if gen == self.gen:
                    self.sig_update.emit(t)
            text = transcribe(wav, _on_partial)
            self.sig_done.emit(text, gen)
        except Exception as e:
            self.sig_error.emit(str(e), gen)

    def on_done(self, text, gen):
        if gen != self.gen: return   # 会话已被重置，丢弃过期结果
        text = (text or "").strip()
        print(f"[语音输入] ✅ {text}", flush=True)
        if text: self._type(text)
        self.state = "idle"; self.sig_hide.emit()

    def on_error(self, msg, gen):
        if gen != self.gen: return   # 会话已被重置，丢弃过期结果
        print(f"[语音输入] ❌ {msg}", flush=True)
        self.sig_update.emit(f"⚠️ {msg}")
        self.state = "idle"
        QtCore.QTimer.singleShot(2500, self.overlay.hide_done)

    def _type(self, text):
        cb = QtWidgets.QApplication.clipboard()
        old = cb.text()
        cb.setText(text)
        time.sleep(0.12)
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"])
        def _restore():
            if old and cb.text() == text: cb.setText(old)
        QtCore.QTimer.singleShot(2000, _restore)


# ───────────────────────── 热键轮询线程 ─────────────────────────
class HotkeyThread(threading.Thread):
    def __init__(self, controller):
        super().__init__(daemon=True); self.ctrl = controller; self.prev = False
    def run(self):
        while True:
            try: km = XLIB.query_keymap()
            except Exception: time.sleep(0.2); continue
            combo = _pressed(km, CTRL_KC) and _pressed(km, SUPER_KC)
            if combo and not self.prev:      self.ctrl.press()
            elif (not combo) and self.prev:  self.ctrl.release()
            self.prev = combo
            time.sleep(0.025)


def main():
    app = QtWidgets.QApplication(sys.argv); app.setApplicationName("VoiceInput")
    if "--demo" in sys.argv:
        ov = Overlay(); ov.show_at("识别中")
        for t in ["你好，这", "你好，这是一段", "你好，这是一段批量语音识别测试。"]:
            ov.set_text(t); app.processEvents(); time.sleep(0.6)
        QtCore.QTimer.singleShot(1500, app.quit); return sys.exit(app.exec_())
    overlay = Overlay(); ctrl = Controller(overlay)
    HotkeyThread(ctrl).start()
    print(f"✅ 语音输入就绪。按住 Ctrl+Win 说话，松开输出。\n   模型 {MODEL} @ {ASR_URL}")
    signal.signal(signal.SIGINT, lambda *a: app.quit())
    t = QtCore.QTimer(); t.start(200)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
