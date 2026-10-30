# 原生文本适配

当前版本锁定 Windows x64 微信 4.1.15.13；使用前核对 `compatibility.json` 中的核心文件指纹。

运行需要的组件：

- `text_service.py`：文件传输助手、好友、群聊的原生文本服务，以及图片事件的异步接收与投递。
- `filehelper_service.py`：接收通道和服务公共实现。
- `run_selftest.py`：进程、版本、远程调用与桥接生命周期封装。
- `image_download.h`：V4 图片 CDN 下载实现与 `WxDownloadImage` 导出，由 `text_bridge.cpp` 包含；旧 V3/V2 DLL 不含该能力。
- `build_text/text_bridge.dll`：当前文本收发和真实艾特桥接；重编后同时导出 `WxPollMediaMessageV4` 与 `WxDownloadImage`，前端才会声明图片接收能力。
- `build/selftest_bridge.dll`：兼容文件传输助手后端。

源码 `text_bridge.cpp` 复用 `selftest_bridge.cpp` 的公共结构、任务调度和清理逻辑，因此两个源文件都要保留。`vendor/` 中只保留编译所需的 MinHook 源码、头文件和许可。

图片下载只接受 32 或 64 位十六进制的 AES 密钥：只带缩略图引用的消息（`cdnthumbaeskey` 为 16 位）由 Python 侧标为 `skipped`，不调用原生接口，也不做猜测性转换。原始图片 XML、CDN 地址与密钥不会进入插件消息。

原始调试器、逆向数据库、跟踪日志、探针可执行文件、旧版本 DLL、编译中间产物和历史版本均不属于本目录。

重新编译使用 `build_bridges.ps1`；正常使用已提供的两个 DLL 时无需编译。账号和群聊从根目录的本地配置读取，没有预置真实用户。
