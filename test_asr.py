#!/usr/bin/env python3
"""Fun-ASR-Realtime 冒烟测试 v2：严格按协议——收到 task-started 后才发音频。"""
import json, time, uuid, sys, threading, os
import numpy as np
import sounddevice as sd
import websocket

CFG = json.load(open(os.path.expanduser("~/.config/voicetype/config.json")))
API_KEY = CFG["api_key"]
WS_URL = "wss://" + CFG["api_host"] + "/api-ws/v1/inference/"   # 专属端点（探测已确认可用）
MODEL = CFG["model"]
TASK_ID = uuid.uuid4().hex[:32]

SR = 16000
DURATION = 4.0
results, errors = [], []
started = threading.Event()
finished = threading.Event()

def on_open(ws):
    ws.send(json.dumps({
        "header": {"action": "run-task", "task_id": TASK_ID, "streaming": "duplex"},
        "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                    "model": MODEL,
                    "parameters": {"sample_rate": SR, "format": "pcm"}, "input": {}}
    }))
    print("→ 已发送 run-task，等待 task-started …", flush=True)

def on_message(ws, data):
    try: m = json.loads(data)
    except Exception: return
    ev = m.get("header", {}).get("event", "")
    if ev == "task-started":
        print("← task-started，开始发送音频", flush=True)
        started.set()
    elif ev == "result-generated":
        s = m.get("payload", {}).get("output", {}).get("sentence", {})
        end = s.get("sentence_end", False)
        results.append(s.get("text", ""))
        print(f"   [{'最终' if end else '中间'}] {s.get('text','')}", flush=True)
    elif ev == "task-finished":
        finished.set(); ws.close()
    elif ev == "task-failed":
        errors.append(m.get("header", {}).get("error_message", str(m)))
        finished.set(); ws.close()

def on_error(ws, e): errors.append("WS-ERR:" + str(e)); finished.set()
def on_close(ws, *a): finished.set()

def feeder(ws, pcm):
    started.wait(timeout=10)            # 关键：等服务端 task-started
    if not started.is_set():
        errors.append("未收到 task-started"); finished.set(); return
    chunk = 3200                         # 100ms @16k mono int16
    for i in range(0, len(pcm), chunk):
        ws.send(pcm[i:i+chunk], opcode=websocket.ABNF.OPCODE_BINARY)
        time.sleep(0.05)                 # 轻微节流，约 2x 实时
    ws.send(json.dumps({"header": {"action": "finish-task", "task_id": TASK_ID, "streaming": "duplex"},
                        "payload": {"input": {}}}))
    print("→ 音频已发完，已发 finish-task", flush=True)

def main():
    print(f"模型: {MODEL}\n端点: {WS_URL}")
    print(f"录音 {DURATION} 秒，准备说话——", flush=True)
    for n in (3, 2, 1):
        print(f"  {n} …", flush=True); time.sleep(0.8)
    print("  🎙️ 开始说！", flush=True)
    audio = sd.rec(int(DURATION * SR), samplerate=SR, channels=1, dtype="int16")
    sd.wait()
    pcm = audio.tobytes()
    rms = float(np.sqrt(np.mean((audio.astype(float) / 32768.0) ** 2)))
    print(f"录音完成：{len(pcm)} 字节，RMS={rms:.4f}", flush=True)

    ws = websocket.WebSocketApp(WS_URL, header={"Authorization": f"bearer {API_KEY}"},
                                on_open=on_open, on_message=on_message,
                                on_error=on_error, on_close=on_close)
    threading.Thread(target=feeder, args=(ws, pcm), daemon=True).start()
    ws.run_forever()
    finished.wait(timeout=3)

    print("\n========== 结果 ==========")
    if errors:
        print("❌ 失败：", "; ".join(errors)); sys.exit(1)
    full = "".join(results).strip()
    print("识别文本：", full if full else "（未识别到内容，检查是否对着麦克风说话）")
    print("✅ 全链路 OK" if full else "⚠️ 连通但无结果")

if __name__ == "__main__":
    main()
