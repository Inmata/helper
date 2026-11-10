# 第三方组件

原生桥接使用 MinHook 1.3.4，并保留构建所需的源码、头文件、作者信息及原始许可证：

- `native/vendor/minhook-1.3.4/LICENSE.txt`
- `native/vendor/minhook-1.3.4/AUTHORS.txt`
- `native/vendor/minhook-1.3.4/src/hde/` 下的原始版权声明

保留的 DLL 由本项目桥接源码和上述组件构建；不包含微信客户端程序或微信核心 DLL。

微信 4.2.0.12 的构建资料和原生地址配置取自 [Prometheus-first/helper 的 b6d35f4](https://github.com/Prometheus-first/helper/commit/b6d35f41c58c9144a06bf39bfa42cab6193cc5c2)，本项目按现有消息桥接接口作了修改。上游许可随附于 [native/UPSTREAM-LICENSE.txt](native/UPSTREAM-LICENSE.txt)。

图片接收的原图优先策略参考 [Prometheus-first/helper 的 5fc55e1](https://github.com/Prometheus-first/helper/commit/5fc55e1c58c69684ae1aee11d03886fe280ff44c)，本项目在原图失败时回退到普通图，继续跳过仅有缩略图引用的消息。上游许可同上。

图片发送适配自 [Prometheus-first/helper 的原生图片发送](https://github.com/Prometheus-first/helper/blob/bb2df75f19ebc00d5f4fefb1c429a215d0c2d09e/native/image_send.h)，沿用已核对构建的地址和图片暂存流程。上游许可同上。

语音接收参考 [Prometheus-first/helper 的 b6d35f4](https://github.com/Prometheus-first/helper/commit/b6d35f41c58c9144a06bf39bfa42cab6193cc5c2) 的内联 SILK 布局。解码依赖 silk-python，转写依赖 sherpa-onnx 和独立安装的 SenseVoice 模型，均未随仓库分发。

语音发送适配自 [Prometheus-first/helper 的原生语音发送](https://github.com/Prometheus-first/helper/blob/be370c10b3ebe0f725fe173517e2c947d9d2f364/native/voice_send.h)及其 [WAV/SILK 准备实现](https://github.com/Prometheus-first/helper/blob/be370c10b3ebe0f725fe173517e2c947d9d2f364/wxbot_framework/voices.py)。本项目改为在内存中编码并交给现有发送队列；WAV 编码沿用 silk-python。上游许可同上。

合并转发的内嵌记录格式参考 [Prometheus-first/helper 的 028b86a](https://github.com/Prometheus-first/helper/commit/028b86af59b17aaa7c0c3f15a9c72716391ff517)，本项目读取文字、原署名和时间。上游许可同上。

成语接龙的扩展词表来自 [pwxcoo/chinese-xinhua](https://github.com/pwxcoo/chinese-xinhua)，Copyright (c) 2018 PWXCOO，MIT License。仅提取四字词条；固定版本、筛选规则与校验值见 `plugins/idiom_chain/data/README.md`，完整许可证见 `plugins/idiom_chain/data/LICENSE.chinese-xinhua.txt`。

`plugins/_shared/stickers/` 中的「笑不活了」「我先化了」「这对吗」「猫猫震惊」「我裂开了」「你继续」「一句都不想说」「我看看怎么个事」「给您鼓掌」「小丑竟是我」「就这」「当场去世」取自 Google 的 [Animated Noto Emoji](https://googlefonts.github.io/noto-emoji-animation/)。本项目提取动画的一帧并加上中文字幕，这些图片适用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。
