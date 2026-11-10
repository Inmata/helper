# 原生文本适配

当前识别 Windows x64 微信 4.1.15.13 和 4.2.0.12 的指定构建。加载前核对客户端版本及 `compatibility.json` 中核心文件的大小和 SHA-256；原生桥接还核对已加载映像的 PE 标识。

4.2.0.12 使用本项目配套构建的 `native_text` 桥接。已实测群文本收发、引用接收、图片下载、退出卸载和重新连接。旧文件传输助手后端和诊断探针保留在 4.1.15.13；新版可通过 `native_text` 与文件传输助手收发文本。

运行需要的组件：

- `text_service.py`：文件传输助手、好友、群聊的原生文本服务，以及图片事件的异步接收与投递。
- `filehelper_service.py`：接收通道和服务公共实现。
- `run_selftest.py`：进程、版本、远程调用与桥接生命周期封装。
- `core_version.h`：原生调用、接收与卸载共用的构建地址配置；`wxbot_framework/native_profiles.py` 记录 Python 读取账号和群成员缓存的对应地址。
- `image_download.h`：V4 图片 CDN 下载实现与 `WxDownloadImage` 导出，由 `text_bridge.cpp` 包含；旧 V3/V2 DLL 不含该能力。
- `build_text/text_bridge.dll`：当前文本收发和真实艾特桥接；重编后同时导出 `WxPollMediaMessageV4` 与 `WxDownloadImage`，前端才会声明图片接收能力。
- `build/selftest_bridge.dll`：兼容文件传输助手后端。

源码 `text_bridge.cpp` 复用 `selftest_bridge.cpp` 的公共结构、任务调度和清理逻辑，因此两个源文件都要保留。`vendor/` 中只保留编译所需的 MinHook 源码、头文件和许可。

图片下载只接受 32 或 64 位十六进制的 AES 密钥：只带缩略图引用的消息（`cdnthumbaeskey` 为 16 位）由 Python 侧标为 `skipped`，不调用原生接口，也不做猜测性转换。原始图片 XML、CDN 地址与密钥不会进入插件消息。

原始调试器、逆向数据库、跟踪日志、探针可执行文件、旧版本 DLL、编译中间产物和历史版本均不属于本目录。

重新编译使用 `build_bridges.ps1`；正常使用已提供的两个 DLL 时无需编译。账号和群聊从根目录的本地配置读取，没有预置真实用户。

## 4.2.0.12 验收与切换

1. 在独立 Windows 环境安装已签名的 4.2.0.12。先用 `Get-FileHash -Algorithm SHA256 -LiteralPath <weixin.dll路径>` 核对核心文件。
2. 使用测试账号和测试群，保持生产配置和数据库独立。先运行 `python -B main.py --check`。
3. 验证私聊和群文本、真实艾特、引用、图片接收与看图，再验证 `quit` 卸载和重连。
4. 用同一套构建在独立环境回归 4.1.15.13。全部通过后再安排切换。回退时正常退出测试版，沿用原有工作目录和 4.1.15.13 环境。
