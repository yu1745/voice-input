#!/usr/bin/env python3
"""诊断版（读缓存文件）：sample.wav → 各端点/模型，打印原始报文。"""
import json, time, uuid, os, threading, wave
import websocket

CFG = json.load(open(os.path.expanduser("~/.config/voicetype/config.json")))
API_KEY = CFG["api_key"]
HERE = os.path.dirname(os.path.abspath(__file__))
PUB = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"
DED = "wss://" + CFG["api_host"] + "/api-ws/v1/inference/"
SR = 16000

# 读 WAV 取 PCM
with wave.open(os.path.join(HERE, "sample.wav"), "rb") as w:
    assert w.getframerate() == SR and w.getsampwidth() == 2 and w.getnchannels() == 1, \
        f"WAV 格式不符: {w.getframerate()}/{w.getsampwidth()}/{w.getnchannels()}"
    PCM = w.readframes(w.getnframes())
print(f"载入 sample.wav: {len(PCM)} 字节 PCM ({len(PCM)/2/SR:.1f}s)\n")

def run_once(label, url, model):
    print(f"===== {label} | {model} =====\n  {url}")
    tid = uuid.uuid4().hex[:32]
    text_out, errs = [], []
    started, done = threading.Event(), threading.Event()

    def on_open(ws):
        ws.send(json.dumps({
            "header": {"action": "run-task", "task_id": tid, "streaming": "duplex"},
            "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                        "model": model,
                        "parameters": {"sample_rate": SR, "format": "pcm"}, "input": {}}}))

    def on_message(ws, data):
        print(f"  ◀ {data[:280]}")
        try: m = json.loads(data)
        except: return
        ev = m.get("header", {}).get("event", "")
        if ev == "task-started":
            started.set()
            def feed():
                for i in range(0, len(PCM), 3200):
                    ws.send(PCM[i:i+3200], opcode=websocket.ABNF.OPCODE_BINARY)
                    time.sleep(0.03)
                ws.send(json.dumps({"header": {"action": "finish-task", "task_id": tid, "streaming": "duplex"},
                                    "payload": {"input": {}}}))
            threading.Thread(target=feed, daemon=True).start()
        elif ev == "result-generated":
            t = m.get("payload", {}).get("output", {}).get("sentence", {}).get("text", "")
            if t: text_out.append(t)
        elif ev == "task-finished": done.set(); ws.close()
        elif ev == "task-failed":
            errs.append(m.get("header", {}).get("error_message", str(m))); done.set(); ws.close()

    def on_error(ws, e): errs.append("WS-ERR:" + str(e)); done.set()
    def on_close(ws, *a): done.set()
    ws = websocket.WebSocketApp(url, header={"Authorization": f"bearer {API_KEY}"},
                                on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    threading.Thread(target=ws.run_forever, daemon=True).start()
    done.wait(timeout=12)
    try: ws.close()
    except: pass
    print(f"  ➡ 识别: {''.join(text_out).strip() or '(空)'} | 错误: {errs or '无'}\n")

run_once("公共端点", PUB, "fun-asr-realtime-2026-02-28")
run_once("公共端点", PUB, "paraformer-realtime-v2")
run_once("专属端点", DED, "fun-asr-realtime-2026-02-28")
