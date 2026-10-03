# 平板试跑版

应用名：播客学伴。原生 Android WebView 外壳，适配平板横竖屏。保持与网页相同的三栏布局：播客、字幕、AI 助教同时可见，左侧播客列表可以手动收起。

手机竖屏（宽度不超过 600 CSS 像素）使用上下同屏布局：上方字幕和播放器，下方 AI 助教。播客列表通过顶部按钮展开，选择单集后收起。Mate 30 Pro 安装示例：`python android/install.py 手机序列号`。

本版通过 USB 调用电脑上的 FastAPI / FFmpeg 服务，尚不支持脱离电脑独立转写。启动电脑服务 `python app.py`，保持 USB 调试连接。重新插拔后运行 `adb reverse tcp:8557 tcp:8557`。

使用 Android SDK 35、JDK 17 或 21、Gradle 8.14.3 构建：`gradle -p android assembleDebug`。在 `android/local.properties` 配置本机 `sdk.dir`。

运行 `python android/install.py` 安装、配置 USB 转发，并将本地 `.env` 的 Groq 和 Gemini Key 注入应用私有目录。应用首次导入后使用 Android Keystore 的 AES-GCM 加密保存，删除临时明文文件。密钥不打入 APK，不放进网页存储。安装脚本不打印密钥。

连接多台设备时，运行 `python android/install.py 设备序列号`，通过 `adb devices -l` 查看序列号。

本地调试 APK 位于 `android/app/build/outputs/apk/debug/app-debug.apk`。应用启动时向电脑服务同步预置配置，网页设置修改保存在电脑端；要更新平板预置密钥，请修改本地 `.env` 后重新运行安装脚本。
