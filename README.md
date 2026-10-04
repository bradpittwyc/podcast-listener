# 🎧 Podcast Listener（播客精听学习助手）

Python FastAPI + HTML/JavaScript 的播客学习工具，支持 Apple Podcasts 榜单、搜索、RSS、同步字幕、语境查词、变速、A–B 循环和句子收藏。Android 手机和平板使用设备端后台，无需电脑服务或 USB。

## 字幕与播放

优先使用 RSS 官方 WebVTT 或完整缓存。需要生成字幕时，Web 和 Android 均使用阿里云北京地域的 `qwen-audio-3.1-asr-flash-streaming`，已移除 Groq 转写入口。

- 下载的数据持续解码为 16 kHz 单声道 PCM，每约 100 毫秒通过 WebSocket 上传，不等待整集下载。Web 使用 FFmpeg，Android 使用 MediaExtractor / MediaCodec。
- 前 5 分钟每 30 秒切换任务，5–15 分钟每 120 秒，之后每 300 秒。两把不同 Key 交替，最多两个任务并行，结果按音频顺序提交。相同 Key 合并为一个通道。
- 使用真实词级时间戳，按句末标点分句。跨任务的未完成句子保留到句末标点或音频结束，不按字数强行拆句，不编造平均时间戳。
- 第一条完整字幕就绪后播放；追上已转写范围时暂停等待。后续字幕不会取消手动暂停。
- 稳定结果先保存断点，再通知页面。断线保留字幕、播放位置和暂停状态，按 1、2、4、8、15 秒退避重连；连续失败 5 次后可手动继续。完整音频缓存可直接定位，否则重新流式解码前缀并跳过已转写部分。
- 连接及任务结束都有超时，心跳不会无限延长结束等待。无效密钥、权限等永久错误不自动重试。取消时清理解码和转写任务。
- 每句旁的旋转箭头可单独重新识别：只上传该句及少量上下文，按词时间戳过滤上下文，替换当前句，保留其他字幕和播放位置。失败保留原文；修正单独保存，后台生成、重连和缓存回放不会覆盖它。
- AI 助教在首条字幕到达后即可提问，发送时携带当前已获得的全部字幕（含单句修正）、提示词、用户问题和勾选例句，不等待整集转写完成。助教默认使用阿里云 Qwen，可在设置中改用 Gemini；查词按其独立模型设置调用。
- 阿里云字幕与旧 Groq 缓存分开。整集刷新清除当前断点和单句修正；音频和完整字幕采用原子缓存。

Web 和平板的助教区可向左展开至屏幕的三分之二，覆盖字幕但不挤压原布局，可按 Esc 收起。手机可通过箭头或双击助教空白区域上下展开阅读；长回答可滚动。分享字幕仍需等整集完成。

## 本地启动

需要 Python 3.10+ 和加入 PATH 的 FFmpeg。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe app.py
```

打开 http://127.0.0.1:8557，在侧边栏设置填写两把阿里云 Key、选择北京。留空保留已保存密钥；设置立即用于后续任务。也可编辑 `.env`，启动环境变量优先。Linux/macOS 使用 `.venv/bin/python`。

| 变量 | 用途 |
| --- | --- |
| `DASHSCOPE_API_KEY_1` / `DASHSCOPE_API_KEY_2` | 两把百炼 Key；第一把兼容 `DASHSCOPE_API_KEY` |
| `ALIYUN_REGION` | 默认 `beijing`，可选 `singapore`，密钥须匹配地域 |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | 查词和助教，默认 `gemini-3.5-flash` |
| `PODCAST_PROXY` | Web 音频下载、官方字幕的 HTTP 代理 |
| `HTTP_PROXY` / `HTTPS_PROXY` | Requests 和 Gemini SDK 的标准代理配置 |
| `HOST` / `PORT` | 默认 `127.0.0.1` / `8557` |

阿里云 WebSocket 使用直接连接。密钥不返回页面、不打入 APK、不得提交到仓库。

## 存储与验证

笔记保存在浏览器 localStorage，可导出 `.txt`。Web 音频、字幕、断点和修正保存在忽略的 `subtitle_cache/`；Android 使用设备私有存储。构建安装说明见 [android/README.md](android/README.md)。

```powershell
python -m pip install httpx
python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/player.test.cjs
```

自动测试使用模拟云响应，覆盖双 Key 并行和顺序、真实 FFmpeg 流式解码、断点续写、句子合并、时间戳、结束超时、永久错误、单句修正及缓存、问答门槛和播放状态。

可选浏览器检查（Playwright + Microsoft Edge）：

```powershell
python -m pip install playwright
python tests/browser_smoke.py
python tests/layout_smoke.py
```

点击字幕栏的「双语字幕」按钮可提交当前字幕翻译，中文逐句显示在原文下方；开启后自动翻译后续字幕，再次点击关闭中文显示。设置中的「翻译模型」可选阿里云 Qwen 或 Gemini（默认 Gemini），分别复用已有阿里云和 Gemini 配置。更换模型或刷新字幕后重新翻译，单句修正只重译变化的句子；翻译失败保留原文，点击按钮重试。翻译不改变播放位置和暂停状态。

错误与操作提示在页面顶部固定显示，不再从底部滑出；Android 分享失败也使用相同提示。助教、翻译和查词的模型选择分别保存。
