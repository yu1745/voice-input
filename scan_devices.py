#!/usr/bin/env python3
"""扫描所有输入设备，各录 0.6 秒测音量，找出真正在收音的麦克风。"""
import sounddevice as sd, numpy as np

print("所有输入设备：")
devs = sd.query_devices()
inputs = [(i, d) for i, d in enumerate(devs) if d['max_input_channels'] > 0]
default_in = sd.default.device[0]
print(f"  默认输入索引 = {default_in}\n")

for i, d in inputs:
    try:
        rec = sd.rec(int(0.6 * d['default_samplerate']), samplerate=d['default_samplerate'],
                     channels=min(d['max_input_channels'], 1), dtype='float32', device=i)
        sd.wait()
        rms = float(np.sqrt(np.mean(rec**2)))
        flag = " ◀ 默认" if i == default_in else ""
        star = " 🎯 有声音!" if rms > 0.01 else ""
        print(f"  [{i}] {d['name']:<28} 速率={d['default_samplerate']}  RMS={rms:.4f}{flag}{star}")
    except Exception as e:
        print(f"  [{i}] {d['name']:<28} 测试失败: {e}")
