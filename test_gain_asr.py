#!/usr/bin/env python3
"""对比：同一段录音，原始音量 vs 归一化放大后，qwen3-asr-flash 识别效果。"""
import os, time, base64, json, wave, io
import numpy as np, requests

CFG = json.load(open(os.path.expanduser("~/.config/voicetype/config.json")))
API_KEY, ASR_URL, MODEL = CFG["api_key"], CFG["asr_url"], CFG["model"]
HERE = os.path.dirname(os.path.abspath(__file__))

with wave.open(os.path.join(HERE, "sample.wav")) as w:
    pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)

peak = float(np.max(np.abs(pcm)))
print(f"原始: peak={peak/32768:.4f} ({20*np.log10(peak/32768+1e-9):.1f} dBFS)\n")

def to_wav_bytes(arr):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(arr.astype(np.int16).tobytes())
    return buf.getvalue()

def asr(wav_bytes, label):
    t0 = time.time()
    data_uri = "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode()
    body = {"model": MODEL,
            "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}]}],
            "stream": False, "asr_options": {"language": "zh", "enable_itn": False}}
    r = requests.post(ASR_URL, timeout=60,
                      headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}, json=body)
    dt = time.time() - t0
    if r.status_code != 200:
        print(f"【{label}】HTTP {r.status_code}: {r.text[:200]}"); return
    print(f"【{label}】{dt:.2f}s\n  → {r.json()['choices'][0]['message']['content']}")

# A: 原始（很安静）
asr(to_wav_bytes(pcm), "原始音量")

# B: 归一化放大到 peak=0.9（约 -1 dBFS）
norm = pcm * (0.9 * 32768 / peak)
asr(to_wav_bytes(norm), "归一化放大后")
