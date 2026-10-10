# 🎧 Podcast Listener（播客精听学习助手）

Python FastAPI + HTML/JavaScript 的播客学习工具，支持 Apple Podcasts 榜单、搜索、RSS、同步字幕、语境查词、变速、A–B 循环和句子收藏。Android 手机和平板使用设备端后台，无需电脑服务或 USB。

## 字幕与播放

优先使用 RSS 官方 WebVTT 或完整缓存。需要生成字幕时，Web 使用 `gemini-3.5-transcribe`，复用 Gemini Key（支持有可用额度的免费 Key）；Android 保持原有阿里云 `qwen-audio-3.1-asr-flash-streaming`。

Web 通过 Interactions API 使用逐字模式与词级时间戳，每段 20 分钟。先下载一次节目音频，再同时转写最多五段，五把 Key 按片段顺序循环分配（0–20、20–40、40–60、60–80、80–100 分钟）；字幕按节目顺序显示。大音频通过 Files API 上传，完成后删除云端临时文件。未结束句子随断点保存，失败时已成功的后续片段也独立缓存，继续时直接复用；单句重识别也使用 Gemini。词时间戳缺失时提示错误，不估算时间。Gemini 字幕、断点和单句修正使用独立缓存，旧阿里云缓存保留。免费额度或速率触发 HTTP 429 时尝试池中其他可用 Key；全部不可用时保留断点，额度恢复后可手动继续。参考：[官方转写文档](https://ai.google.dev/gemini-api/docs/transcribe)。

- 下载的数据持续解码为 16 kHz 单声道 PCM，不等待整集下载。Web 使用 FFmpeg 并将有界片段封装为 WAV 提交 Gemini；Android 保持 MediaExtractor / MediaCodec 与原有阿里云 WebSocket 流程。
- Web 同时最多提交五个 Gemini 转写请求，按音频顺序保存字幕；首段完成后开始播放。
- Web 使用真实词级时间戳，按句末标点、超过 1.2 秒的停顿或 15 秒字幕长度分句。跨片段的未完成句子随断点保留，不编造平均时间戳。
- 第一条完整字幕就绪后播放；追上已转写范围时暂停等待。后续字幕不会取消手动暂停。
- 稳定结果先保存断点，再通知页面。断线保留字幕、播放位置和暂停状态，按 1、2、4、8、15 秒退避重连；连续失败 5 次后可手动继续。Android 解码从断点附近定位，完整 Web 音频缓存可直接定位；Web 未完成缓存时仍需解码前缀，但真实解码进度会维持连接，避免被误判为转写停滞。
- 连接及任务结束都有超时，心跳不会无限延长结束等待。无效密钥、权限等永久错误不自动重试。取消时清理解码和转写任务。
- 每句旁的旋转箭头可单独重新识别：只上传该句及少量上下文，按词时间戳过滤上下文，替换当前句，保留其他字幕和播放位置。失败保留原文；修正单独保存，后台生成、重连和缓存回放不会覆盖它。
- AI 助教在首条字幕到达后即可提问，发送时携带当前已获得的全部字幕（含单句修正）、提示词、用户问题和勾选例句，不等待整集转写完成。助教默认使用阿里云 Qwen，可在设置中改用 Gemini；查词按其独立模型设置调用。
- 阿里云字幕与旧 Groq 缓存分开。整集刷新清除当前断点和单句修正；音频和完整字幕采用原子缓存。服务启动时清理闲置节目缓存：Web 上限 2 GiB、Android 转写缓存上限 64 MiB，超过 30 天的闲置缓存也会清理；设置、凭据和笔记不参与清理。
- Android 冷启动停在首页，手动选择旧节目时恢复该节目的进度和倍速。播放结束不会因返回前台而重播；主动点击播放才重新开始。前台服务保留进程内播放器，Activity 重建时复用，结束整个进程后仍重新进入首页。
- 音频网络故障独立重试并保留位置，最多自动恢复六次；手动暂停或切换节目会取消恢复。字幕增量只更新改变的行，避免整集 DOM 反复重建。

Web 和平板的助教区可向左展开至屏幕的三分之二，覆盖字幕但不挤压原布局，可按 Esc 收起。手机可通过箭头或双击助教空白区域上下展开阅读；长回答可滚动。分享字幕仍需等整集完成。

## 本地启动

需要 Python 3.10+ 和加入 PATH 的 FFmpeg。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe app.py
```

打开 http://127.0.0.1:8557，在侧边栏设置填写 Gemini Key 即可使用 Web 转写；阿里云 Key 保留供查词、翻译和助教使用。留空保留已保存密钥；设置立即用于后续任务。也可编辑 `.env`，启动环境变量优先。Linux/macOS 使用 `.venv/bin/python`。

| 变量 | 用途 |
| --- | --- |
| `DASHSCOPE_API_KEY_1` / `DASHSCOPE_API_KEY_2` | 两把百炼 Key；第一把兼容 `DASHSCOPE_API_KEY` |
| `ALIYUN_REGION` | 默认 `beijing`，可选 `singapore`，密钥须匹配地域 |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Key 用于 Web 转写和 Gemini 功能；转写固定 `gemini-3.5-transcribe`，查词和助教默认 `gemini-3.5-flash` |
| `GEMINI_TRANSCRIPTION_KEY_1` … `GEMINI_TRANSCRIPTION_KEY_5` | Web 专用转写 Key 池，按 20 分钟片段循环使用；未配置时复用 `GEMINI_API_KEY` |
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
