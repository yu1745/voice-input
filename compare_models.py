#!/usr/bin/env python3
"""同一段 sample.wav 在各候选实时 ASR 模型上的 A/B 对比。"""
import json, time, uuid, os, threading, wave
import numpy as np, websocket
from websocket import ABNF

CFG = json.load(open(os.path.expanduser("~/.config/voicetype/config.json")))
API_KEY, WS_URL = CFG["api_key"], CFG["ws_url"]
SR = 16000
HERE = os.path.dirname(os.path.abspath(__file__))

with wave.open(os.path.join(HERE, "sample.wav"), "rb") as w:
    PCM = w.readframes(w.getnframes())
# RMS
arr = np.frombuffer(PCM, dtype=np.int16).astype(float) / 32768.0
print(f"sample.wav: {len(PCM)} 字节 ({len(PCM)//2//SR:.1f}s)  RMS={np.sqrt(np.mean(arr**2)):.4f}" +
      ("  ⚠️音量偏低（建议提高麦克风增益/靠近麦克风）\n" if np.sqrt(np.mean(arr**2)) < 0.03 else "  音量正常\n"))

MODELS = [
    "fun-asr-realtime-2026-02-28",      # 当前
    "qwen-audio-3.0-asr-flash-streaming",
    "paraformer-realtime-v2",
    "qwen3-asr-flash-realtime",         # 新版 Qwen3（可能协议不同）
]

def run(model):
    tid = uuid.uuid4().hex[:32]
    finals, errs = {}, []
    started, done = threading.Event(), threading.Event()
    def on_open(ws):
        ws.send(json.dumps({"header": {"action":"run-task","task_id":tid,"streaming":"duplex"},
                            "payload": {"task_group":"audio","task":"asr","function":"recognition",
                                        "model":model,"parameters":{"sample_rate":SR,"format":"pcm"},"input":{}}}))
    def on_msg(ws, data):
        try: m=json.loads(data)
        except: return
        ev=m.get("header",{}).get("event","")
        if ev=="task-started": started.set()
        elif ev=="result-generated":
            s=m.get("payload",{}).get("output",{}).get("sentence",{})
            if s.get("sentence_end"): finals[s.get("sentence_id")]=s.get("text","")
        elif ev=="task-finished": done.set(); ws.close()
        elif ev=="task-failed": errs.append(m.get("header",{}).get("error_message","")); done.set(); ws.close()
    def on_err(ws,e): errs.append("WS:"+str(e)); done.set()
    ws=websocket.WebSocketApp(WS_URL, header={"Authorization":f"bearer {API_KEY}"},
                              on_open=on_open,on_message=on_msg,on_error=on_err,on_close=lambda *a:done.set())
    def feed():
        started.wait(timeout=8)
        if not started.is_set(): done.set(); return
        for i in range(0,len(PCM),3200):
            try: ws.send(PCM[i:i+3200],opcode=ABNF.OPCODE_BINARY)
            except: break
            time.sleep(0.03)
        try: ws.send(json.dumps({"header":{"action":"finish-task","task_id":tid,"streaming":"duplex"},"payload":{"input":{}}}))
        except: pass
    threading.Thread(target=feed,daemon=True).start()
    threading.Thread(target=ws.run_forever,daemon=True).start()
    done.wait(timeout=15)
    try: ws.close()
    except: pass
    return "".join(finals[k] for k in sorted(finals)), errs

for m in MODELS:
    txt, err = run(m)
    print(f"\n【{m}】")
    if err: print("  ❌", "; ".join(err))
    else:   print("  →", txt if txt else "(无结果)")
