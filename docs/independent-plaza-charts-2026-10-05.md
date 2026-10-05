# 独立学伴 US / UK 播客广场

本次只修改独立 podcast-listener 的 Web 和 mobile-ui 工作树，不涉及 Tony 项目。

来源为 US / UK 榜单网页的 2026-10-05 最新快照，存放于 static/podcast-charts/us.json 与 gb.json。各有 17 个分类，包含节目封面、排名、作者、双语简介和来源收录的单集。英国部分分类为 29 项，按来源保留。点播沿用独立学伴原有字幕及播放流程；其他国家榜单、RSS 搜索及笔记功能保留。US / UK 分类选择在返回广场时保留，单集按来源顺序展示。

Web 服务当前使用 http://127.0.0.1:8559/。Android 将榜单 JSON 随 APK 打包，经设备内本地服务提供，不需要电脑或在线榜单 API。NativeBackend 的 JSON 资源采用 application/json 类型，并允许读取完整快照。

验证：Web 播放器 55 项测试、mobile-ui 播放器 63 项测试通过；Android assembleDebug 与 testDebugUnitTest 通过；浏览器检查 US / UK、分类切换、节目双语简介、十个单集和手机布局通过。没有在验证中调用模型接口。Pixel 已原位安装并启动，设备内服务提供的两份榜单均已校验；华为 LIO-AL00 未连接，未更新；没有更新平板。
