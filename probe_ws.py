#!/usr/bin/env python3
"""探测哪个 WebSocket URL + 模型组合可用。不发音频，只握手看第一个事件。"""
import json, time, uuid, threading
import websocket
import os

os.environ.setdefault("PYTHONHTTPSVERIFY", "1")
CFG = json.load(open(os.path.expanduser("~/.config/voicetype/config.json")))
API_KEY = CFG["api_key"]
DED = CFG["api_host"]  # llm-xkywogi6jayi0yb7.cn-beijing.maas.aliyuncs.com

CANDIDATES = [
    ("公共 + 斜杠",   "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"),
    ("公共 + 无斜杠", "wss://dashscope.aliyuncs.com/api-ws/v1/inference"),
    ("专属 + 斜杠",   f"wss://{DED}/api-ws/v1/inference/"),
    ("专属 + 无斜杠", f"wss://{DED}/api-ws/v1/inference"),
]
MODELS = ["fun-asr-realtime-2026-02-28", "paraformer-realtime-v2",
          "qwen-audio-3.0-asr-flash-streaming", "paraformer-realtime-8k-v2"]

def probe(url, model, timeout=8):
    tid = uuid.uuid4().hex[:32]
    first = {"got": None}
    def on_open(ws):
        ws.send(json.dumps({
            "header": {"action": "run-task", "task_id": tid, "streaming": "duplex"},
            "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                        "model": model,
                        "parameters": {"sample_rate": 16000, "format": "pcm"}, "input": {}}
        }))
    def on_message(ws, data):
        if first["got"] is None:
            try: first["got"] = json.loads(data).get("header", {}).get("event", "") + " :: " + json.loads(data).get("header", {}).get("error_message", "")
            except: first["got"] = data[:120]
        ws.close()
    def on_error(ws, e): first["got"] = "ERR:" + str(e)[:120]
    def on_close(ws, *a): pass
    ws = websocket.WebSocketApp(url, header={"Authorization": f"bearer {API_KEY}"},
                                on_open=on_open, on_message=on_message,
                                on_error=on_error, on_close=on_close)
    t = threading.Thread(target=ws.run_forever, daemon=True); t.start()
    t.join(timeout)
    try: ws.close()
    except: pass
    return first["got"] or "TIMEOUT(无响应)"

for label, url in CANDIDATES:
    print(f"\n### {label}\n  {url}")
    for m in MODELS:
        r = probe(url, m)
        tag = "✅" if r and r.startswith("task-started") else ("❌" if r and ("fail" in r.lower() or "ERR" in r or "url error" in r.lower()) else "❓")
        print(f"  {tag} {m:42} → {r}")
