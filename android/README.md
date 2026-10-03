# Android 独立版

应用名：播客学伴。原生 Android WebView 外壳，适配平板横竖屏。保持与网页相同的三栏布局：播客、字幕、AI 助教同时可见，左侧播客列表可以手动收起。

手机竖屏（宽度不超过 600 CSS 像素）使用上下同屏布局：上方字幕和播放器，下方 AI 助教。播客列表通过顶部按钮展开，选择单集后收起。Mate 30 Pro 安装示例：`python android/install.py 手机序列号`。

下载、RSS 解析、音频解码、切片、Groq 转写、Gemini 问答和字幕缓存均在 Android 设备上运行。无需电脑服务或 USB 转发；设备需要能访问播客源、Groq 和 Gemini 的网络。界面和图标字体随 APK 打包。

Android MediaExtractor / MediaCodec 从远程音频流持续解码，转为 16 kHz 单声道 WAV 切片。前 5 分钟每片 30 秒，5–15 分钟每片 120 秒，之后每片 300 秒。两个 Groq Key 的独立工作线程交替处理切片，结果按音频顺序提交，首段字幕就绪后播放。设备私有目录原子保存已完成切片和字幕；断线重连从最后完成的切片继续，跳过已转写的音频。字幕完整缓存后直接复用。Gemini 问答要求完整字幕并包含全文。

当前 Gemini 模型为 `gemini-3.5-flash`。设备上存在 `127.0.0.1:7890` 本机 HTTP 代理时，云 API 请求使用该代理的 HTTPS CONNECT 隧道；TLS 证书仍正常验证。没有该端口时使用设备直接网络/VPN。全局代理模式并不保证当前出口能访问云服务，403 仍需检查代理出口或服务授权。音频源使用设备的正常媒体网络。

使用 Android SDK 35、JDK 17 或 21、Gradle 8.14.3 构建：`gradle -p android assembleDebug`。在 `android/local.properties` 配置本机 `sdk.dir`。

运行 `python android/install.py` 安装，并将本地 `.env` 的 Groq 和 Gemini Key 注入应用私有目录。应用首次导入后使用 Android Keystore 的 AES-GCM 加密保存，删除临时明文文件。密钥不打入 APK，不放进网页存储。安装脚本不打印密钥，并移除旧版 USB 转发。

连接多台设备时，运行 `python android/install.py 设备序列号`，通过 `adb devices -l` 查看序列号。

本地调试 APK 位于 `android/app/build/outputs/apk/debug/app-debug.apk`。应用设置中修改的密钥加密保存在当前设备，重启后继续有效。重新运行安装脚本会将电脑 `.env` 中的密钥重新预置到指定设备。

电脑网页仍使用原 Python 后台；Android 使用自己的本机后台，两端字幕缓存独立。
