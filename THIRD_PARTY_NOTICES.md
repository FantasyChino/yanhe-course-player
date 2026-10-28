# 第三方与外部资源说明

## HLS.js

- 项目地址：[video-dev/hls.js](https://github.com/video-dev/hls.js)
- 本次引入版本：1.7.3
- 官方发布：[v1.7.3](https://github.com/video-dev/hls.js/releases/tag/v1.7.3)
- 上游许可：Apache License 2.0，见[上游 LICENSE](https://github.com/video-dev/hls.js/blob/v1.7.3/LICENSE)
- 使用位置：`dist/vendor/hls.min.js`，用于浏览器中的在线 HLS 播放。
- SHA-256：`a12e7ee1cd64a69dcdb314157e45dafcba705bfb0b1440b7935cb265d374423e`

该库随播放器本地发行，无需运行时从 CDN 加载。版权与许可归其原权利人，Dailymotion、Brightcove 等上游声明保留在 [hls.js-LICENSE.txt](licenses/hls.js-LICENSE.txt)，许可全文见 [Apache-2.0.txt](licenses/Apache-2.0.txt)。本项目独立代码的 MIT 许可不替代 HLS.js 的许可。

## BIT_yanhe_download

- 项目名称：BIT_yanhe_download
- 项目地址：[BITNP/BIT_yanhe_download](https://github.com/BITNP/BIT_yanhe_download)
- 原作者：[AuYang261](https://github.com/AuYang261/BIT_yanhe_download)
- 上游版权行：`Copyright (c) 2023 AuYang261`
- 维护组织：BITNP
- 上游许可：MIT，见[上游 LICENSE 原文](https://github.com/BITNP/BIT_yanhe_download/blob/main/LICENSE)

同频参考该项目所介绍的延河课堂摄像头与屏幕信号下载使用方式，推荐用户先按上游说明准备视频，再用本地播放器观看。下载器的安装、登录、GUI 与下载选项请以上游最新文档为准。

播放器独立代码使用本项目的 MIT 许可；在线适配在签名、媒体路径等协议处理处参考了上游实现，并保留其作者与 MIT 许可归属，完整原文见 [BIT_yanhe_download-MIT.txt](licenses/BIT_yanhe_download-MIT.txt)。本仓库不捆绑上游下载器可执行程序、课程内容或用户登录凭据。对上游项目的引用不表示双方存在隶属、维护或背书关系。

## 课程编号参考表

- 外部入口：[腾讯文档课程编号参考表](https://docs.qq.com/sheet/DWkJqem5OWVF1Qmxq?tab=000001)

该表格由外部维护。本项目只提供链接，不维护、复制、同步或分发其中的课程编号数据，也不保证表格完整性或可用性。

## 延河课堂

- 平台地址：[延河课堂](https://www.yanhekt.cn/)

延河课堂是课程查找、登录与访问的平台，与本项目独立。平台账号、访问权限、课程内容及其使用约定由相应权利人与平台管理。本项目不提供额外访问授权。

## 本地运行依赖

Python 与用户自行安装的浏览器是运行环境，本仓库不捆绑它们的安装包。前端使用 HTML、CSS 与 JavaScript，并使用上面列出的 HLS.js 播放在线信号；课程服务使用 Python 标准库。项目独立代码由 FantasyChino 以 MIT 许可证发布，完整条款见 [LICENSE](LICENSE)。
