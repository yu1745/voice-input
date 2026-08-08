#!/usr/bin/env python3
"""录一次样本音频，存为 WAV + PCM，供后续所有测试复用。"""
import os, time, wave
import numpy as np
import sounddevice as sd

SR, DUR = 16000, 8.0
OUT = os.path.dirname(os.path.abspath(__file__))

print(f"🎙️  录 {DUR:.0f} 秒样本，准备说话——")
for n in (3, 2, 1):
    print(f"  {n} …"); time.sleep(1.5)
print("  🗣️ 开始说（随便说一句中文，比如「你好，这是一段语音输入测试」）！")
audio = sd.rec(int(DUR * SR), samplerate=SR, channels=1, dtype="int16")
sd.wait()
pcm = audio.tobytes()
rms = float(np.sqrt(np.mean((audio.astype(float)/32768.0)**2)))

wav_path = os.path.join(OUT, "sample.wav")
with wave.open(wav_path, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes(pcm)
with open(os.path.join(OUT, "sample.pcm"), "wb") as f:
    f.write(pcm)

print(f"\n✅ 已保存：{wav_path}（{len(pcm)} 字节，{DUR:.0f}s）")
print(f"   RMS 音量 = {rms:.4f}" + ("  ⚠️ 偏低，建议重录" if rms < 0.02 else "  OK"))
