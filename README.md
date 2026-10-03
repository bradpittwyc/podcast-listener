# 🎧 Podcast Listener（播客精听学习助手）

一个 Python FastAPI + 原生 HTML/JavaScript 的英语播客学习工具。支持 Apple Podcasts 榜单、搜索、RSS 导入、同步字幕、语境查词、变速、A–B 循环和句子收藏。

## 字幕与播放

优先读取 RSS 中的官方 WebVTT 字幕或完整本地缓存，否则使用 Groq `whisper-large-v3-turbo` 生成字幕。

- 音频保持暂停，第一段字幕生成完成后才开始播放。
- 前 5 分钟每 30 秒转写一段；第 5–15 分钟每 2 分钟一段；15 分钟后每 5 分钟一段。
- 后续字幕在后台生成。播放或跳转追上已转写范围时暂停等待，下一段字幕就绪后继续。
- 手动暂停不会被新字幕恢复播放；切换节目会关闭旧字幕连接。
- 首次转写仍需先下载完整音频，再提取第一段。字幕就绪后播放本地音频，避免转写过程中切换音频源。
- 音频缓存以完整下载后原子替换的方式写入；字幕仅在整集转写成功后保存。空字幕缓存会重新生成。
- 点击 Refresh 会跳过旧字幕缓存，重新下载音频并转写。转写失败不会自动播放无字幕音频。

Groq 使用 16 kHz、单声道 WAV 切片并返回句级时间戳。最长 5 分钟切片约 9.6 MB；偏移根据实际音频样本长度计算。限流和服务器错误会有限重试，字幕连接定期发送心跳。

## 本地启动

需要 Python 3.10+ 和 FFmpeg（命令行可运行 `ffmpeg -version`）。

```powershell
git clone https://github.com/bradpittwyc/podcast-listener.git
cd podcast-listener
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`：设置 `GROQ_API_KEY`；如需 AI 中英查词，再设置 `GEMINI_API_KEY`。不要提交密钥，`.env` 已在 Git 忽略列表中。环境变量优先于 `.env`。

```powershell
.\.venv\Scripts\python.exe app.py
```

打开 http://127.0.0.1:8557。Linux/macOS 可使用 `.venv/bin/python` 和 `cp .env.example .env`。

可选配置：

| 变量 | 用途 |
| --- | --- |
| `GEMINI_MODEL` | 查词模型，默认 `gemini-2.5-flash` |
| `PODCAST_PROXY` | 后端转写使用的 HTTP 代理，例如 `http://127.0.0.1:7890` |
| `HTTP_PROXY` / `HTTPS_PROXY` | Requests 和 Gemini SDK 的标准代理配置 |
| `HOST` / `PORT` | 默认 `127.0.0.1` / `8557` |

不再扫描本地端口猜测代理协议。需要代理时请显式配置；Gemini SDK 使用标准代理变量。

## 数据存储

句子笔记保存在当前浏览器的 localStorage，可导出 `.txt`。音频和完整字幕保存在 `subtitle_cache/`；转写临时切片结束或取消后自动清理。本项目会下载并存储音频，使用时请遵守节目来源的使用条款。

## 验证

```powershell
python -m pip install httpx
python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/player.test.cjs
```

后端测试覆盖缓存、失败下载、错误 SSE、Groq 限流重试、时间偏移、取消和真实 FFmpeg 分段（未安装 FFmpeg 时跳过该项）。前端测试覆盖首段等待、字幕进度边界、手动暂停、刷新和切换节目。测试使用模拟 Groq 响应，不调用付费 API。

可选真实浏览器播放检查（需要 Playwright 和 Microsoft Edge）：

```powershell
python -m pip install playwright
python tests/browser_smoke.py
```
