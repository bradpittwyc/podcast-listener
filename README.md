# 🎧 Podcast Listener (播客沉浸式听力学习助手)

Podcast Listener 是一个专注于全球英语播客精听与外语学习的开源 Web 应用。通过直接流式播放创作者原始 RSS 链接，结合 **Whisper AI 实时字幕生成** 与 **Gemini 3.5 Flash 语境词箱**，为外语学习者提供沉浸式、极爽的精听与查词体验。

---

## ✨ 核心特性

- 🌐 **全球 9 国热门播客榜单**：一键探索 US / GB / AU / CA / JP / DE / FR / ES / KR 的 Apple Podcasts Top 30 官方榜单。
- 🔍 **智能搜索与直连**：支持关键词搜索、直接粘贴 Apple Podcast 链接或任意 RSS 订阅源。
- 📜 **逐字同步 AI 字幕**：
  - 官方 CC (VTT) 自动适配
  - 自动运行 **Whisper AI (base)** 实时语音转写
  - 播放卡拉OK高亮与点击字幕行跳转播放
- 🧠 **Gemini AI 语境查词**：
  - 点击字幕中任意单词，自动提取当前播放字幕行作为上下文发送给 **Gemini AI**
  - 输出中英文定义、音标、例句，以及**特定播客语境下的精确词义分析**
  - 智能防遮挡弹窗自动反转与边缘定位
- 🎛️ **精听控制台**：
  - 变速播放（0.6×, 0.75×, 1.0×, 1.25×, 1.5×, 2.0×）
  - A-B 段落无限循环（Set A / Set B / Loop）
  - 逐句书签收藏 (`🔖`) 存入本地笔记本并支持导出 `.txt`
- 🛡️ **100% 著作权合规**：
  - 音频流直接调用创作者原始 RSS CDN（`audio.src = ep.audioUrl`），零存储、零转存、保留创作者完整版权。

---

## 🚀 快速开始

### 1. 克隆仓库与安装依赖

```bash
git clone https://github.com/bradpittwyc/podcast-listener.git
cd podcast-listener

# 安装依赖
pip install -r requirements.txt
```

### 2. 启动服务

```bash
python app.py
```

服务默认在 `http://127.0.0.1:8557` (或 `http://0.0.0.0:8557`) 启动，在浏览器中打开即可开启极爽的播客精听体验！

---

## 📄 开源协议

MIT License
