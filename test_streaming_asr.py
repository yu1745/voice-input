#!/usr/bin/env python3
"""同一段干净音频 sample2.wav → 流式 fun-asr-realtime-2026-02-28 识别（模拟实时）。"""
import json, time, uuid, os, threading, wave
import websocket
from websocket import ABNF

CFG = json.load(open(os.path.expanduser("~/.config/voicetype/config.json")))
API_KEY = CFG["api_key"]
WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"
MODEL = "fun-asr-realtime-2026-02-28"
SR = 16000

with wave.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample2.wav")) as w:
    assert w.getframerate() == SR and w.getsampwidth() == 2 and w.getnchannels() == 1
    PCM = w.readframes(w.getnframes())
print(f"sample2.wav: {len(PCM)} 字节 ({len(PCM)//2//SR:.1f}s)")
print(f"真实文本: 好了，但是我希望还是要恢复剪贴板。把延时设的长一些。毕竟我不可能连续的快速的进行多次语音输入\n")

def run(model):
    tid = uuid.uuid4().hex[:32]
    finals, errs = {}, []
    started, done = threading.Event(), threading.Event()
    t0 = time.time()
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
        # 按真实速率发送（模拟实时录音）
        for i in range(0,len(PCM),3200):
            try: ws.send(PCM[i:i+3200],opcode=ABNF.OPCODE_BINARY)
            except: break
            time.sleep(0.1)
        try: ws.send(json.dumps({"header":{"action":"finish-task","task_id":tid,"streaming":"duplex"},"payload":{"input":{}}}))
        except: pass
    threading.Thread(target=feed,daemon=True).start()
    threading.Thread(target=ws.run_forever,daemon=True).start()
    done.wait(timeout=len(PCM)//2//SR + 8)
    try: ws.close()
    except: pass
    dt = time.time()-t0
    return "".join(finals[k] for k in sorted(finals)), errs, dt

txt, err, dt = run(MODEL)
print(f"【流式 {MODEL}】总耗时 {dt:.2f}s")
if err: print("  ❌", "; ".join(err))
else:   print("  →", txt if txt else "(无结果)")
