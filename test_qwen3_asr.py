#!/usr/bin/env python3
"""测试 qwen3-asr-flash（批量/全局优化）OpenAI 兼容接口：base64 本地音频 → 文本。"""
import os, time, base64, json, requests

CFG = json.load(open(os.path.expanduser("~/.config/voicetype/config.json")))
API_KEY = CFG["api_key"]
HERE = os.path.dirname(os.path.abspath(__file__))
DED = f"https://{CFG['api_host']}/compatible-mode/v1/chat/completions"
PUB = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

# base64 编码 sample.wav
wav = open(os.path.join(HERE, "sample.wav"), "rb").read()
data_uri = "data:audio/wav;base64," + base64.b64encode(wav).decode()
print(f"sample.wav: {len(wav)} 字节 → base64 {len(data_uri)} 字符\n")

def call(url, label, language=None):
    body = {
        "model": "qwen3-asr-flash",
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": data_uri}}]}],
        "stream": False,
        "asr_options": {"enable_itn": False},
    }
    if language: body["asr_options"]["language"] = language
    t0 = time.time()
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                          json=body, timeout=60)
        dt = time.time() - t0
        if r.status_code != 200:
            print(f"【{label}】HTTP {r.status_code}: {r.text[:300]}"); return
        d = r.json()
        text = d["choices"][0]["message"]["content"]
        ann = d["choices"][0]["message"].get("annotations", [])
        lang = next((a.get("language") for a in ann if a.get("type") == "audio_info"), "?")
        print(f"【{label}】{dt:.2f}s  语种={lang}\n  → {text}")
    except Exception as e:
        print(f"【{label}】异常: {e}")

call(PUB, "公共端点")
call(DED, "专属端点")
call(PUB, "公共端点+指定zh", language="zh")
