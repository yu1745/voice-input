# Voice Input

Linux 语音输入法：按住 `Ctrl + Win` 说话，松开即把识别结果粘贴到当前光标。屏幕底部半透明窗口实时显示识别进度。

## 特性

- **全局热键**：`Ctrl + Win` 按住说话 / 松开输出（python-xlib 轮询，无需 root）
- **双模型架构**：
  - 录音中 → `fun-asr-realtime`（WebSocket 流式）实时出字预览
  - 松开后 → `qwen3-asr-flash`（批量，全局优化）作为**最终结果**粘贴
- **半透明悬浮窗**：屏幕底部实时反馈，不抢焦点
- **CJK 友好粘贴**：剪贴板 + `xdotool Ctrl+V`，2 秒后自动恢复原剪贴板
- **systemd 用户服务**：开机自启、崩溃自动重启

## 环境要求

- Linux + X11（已在 Ubuntu 24.04 / KDE 测试；Wayland 需自行适配）
- Python 3.10+
- 系统依赖：`portaudio`、`xdotool`
  ```bash
  sudo apt install libportaudio2 xdotool   # Debian/Ubuntu
  ```
- 阿里云百炼（DashScope）API Key：https://bailian.console.aliyun.com/?tab=api-key#/api-key

## 安装

```bash
git clone https://github.com/yu1745/voice-input.git
cd voice-input
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 配置 API Key（复制模板后填入你的 Key）
mkdir -p ~/.config/voicetype
cp config.example.json ~/.config/voicetype/config.json
# 编辑 ~/.config/voicetype/config.json，把 api_key 改成你的 sk-...
```

## 运行

```bash
.venv/bin/python voice_input.py
```

按住 `Ctrl + Win` 说话，松开即可。

## 安装为 systemd 服务（开机自启）

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/voice-input.service <<EOF
[Unit]
Description=Voice Input (qwen3-asr-flash, Ctrl+Win push-to-talk)
After=graphical-session.target pipewire.service

[Service]
Type=simple
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python $(pwd)/voice_input.py
Restart=on-failure
RestartSec=3
Environment=DISPLAY=:0
Environment=XAUTHORITY=%h/.Xauthority
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

loginctl enable-linger "$USER"          # 用户实例开机自启、常驻
systemctl --user daemon-reload
systemctl --user enable --now voice-input.service
```

管理命令：
```bash
systemctl --user status voice-input     # 状态
systemctl --user restart voice-input    # 重启（改代码后）
journalctl --user -u voice-input -f     # 实时日志
```

## 配置说明

配置文件：`~/.config/voicetype/config.json`（**含密钥，请勿提交 git**，已在 .gitignore）

| 字段 | 说明 | 默认 |
|---|---|---|
| `api_key` | 百炼 API Key | — |
| `asr_url` | 批量识别接口 | `.../compatible-mode/v1/chat/completions` |
| `model` | 批量模型（最终结果） | `qwen3-asr-flash` |
| `language` | 语种提示 | `zh` |
| `gain` | 软件增益倍数（麦克风偏弱时调大） | `4.0` |

> 实时预览用的是 `fun-asr-realtime-2026-02-28`，写死在代码里，无需配置。

## 工作原理

```
按住 Ctrl+Win ──────────────────────────── 松开
   │                                         │
   ├─ sounddevice 录音                       ├─ 停止录音，得到完整 WAV
   ├─ 同时 WebSocket 推流给 fun-asr-realtime  ├─ WAV base64 → qwen3-asr-flash 批量
   └─ 实时返回文字 → 悬浮窗显示                └─ 全局优化结果 → 剪贴板 → Ctrl+V → 2s 恢复
        （预览，可能不准）                          （最终结果，粘贴这个）
```

## 分支说明

- `main` — 当前版本，基于阿里云 qwen3-asr-flash / fun-asr-realtime
- `legacy` — 早期版本（豆包 Seed ASR / 华为云），已封存，不再维护

## License

MIT
