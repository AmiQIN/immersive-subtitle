# Immersive Subtitle

> Real-time context capture for language learners — Oversub + SceneTalk.

面向语言学习者的一组 Windows 桌面小工具：一个把听到的内容实时转成字幕，让人边看边泡在目标语言里；一个把脑子里冒出来的中文换成地道说法，并按遗忘曲线攒起来复习。

## 两个组件

| | **Oversub 沉浸字幕** | **SceneTalk 情景译** |
| --- | --- | --- |
| 解决什么 | 听懂：看视频、打游戏时的实时双语字幕 | 会说：该场景下母语者真会说的英文 |
| 形态 | PySide6 桌面客户端，可置顶的悬浮字幕条 | Edge 应用窗口 + 本机静态小服务，单文件前端 |
| 链路 | 系统声音回采（loopback）→ Whisper 识别 → 离线/在线翻译（中↔英双向）→ 悬浮字幕 | 中文输入（麦克风或打字）→ 英文主句 + 两条替换说法 + 读法提示 → 语料库 |
| 学习闭环 | 生词本、收藏、音频切片、记录导出、跟读朗读 | 语料库、间隔复习（1 / 3 / 7 / 16 / 35 / 90 天）、JSON 备份 |
| 生成引擎 | 本机 faster-whisper 识别 + Argos 离线翻译；可切在线模型 | 内置 60 句 / 12 情境离线语料；可接 OpenAI 兼容接口或本地 Ollama |
| 详细文档 | [`Oversub/README.md`](Oversub/README.md) | [`SceneTalk/README.md`](SceneTalk/README.md) |

## 快速开始

### SceneTalk 情景译

1. 本机装有 Python 和 Edge 浏览器。
2. 双击 `SceneTalk/启动SceneTalk.cmd`，会弹出一个独立应用窗口（不是浏览器标签页）。
3. 左边选情境 → 说中文或打字 → 右边生成英文；句子自动进入语料库和复习队列。

页面、复习规则、引擎配置与已知限制见 [`SceneTalk/README.md`](SceneTalk/README.md)。

### Oversub 沉浸字幕

- **免安装绿色包**：在 [Releases](https://github.com/AmiQIN/immersive-subtitle/releases) 下载最新压缩包，解压后双击 `Oversub.exe` 即可运行，无需另装 Python 和依赖；识别 / 翻译模型首次使用时按提示下载。
- **从源码运行**：在装好依赖的 Python 环境中运行 `Oversub/oversub.py`，或双击 `Oversub/启动沉浸字幕.cmd`。
- 打包产物、运行库、模型和缓存体积较大，均不纳入仓库（见 `.gitignore`）。
- 界面操作、快捷键、学习功能、导出与已知限制见 [`Oversub/README.md`](Oversub/README.md)。

## 仓库结构

```
immersive-subtitle/
├── Oversub/                 # 沉浸字幕：实时双语字幕客户端
│   ├── oversub.py           # 主程序源码（单文件）
│   ├── oversub3.ico         # 程序图标
│   ├── README.md            # 组件详细说明
│   └── 启动沉浸字幕.cmd      # 源码启动入口
├── SceneTalk/               # 情景译：情境表达 + 间隔复习
│   ├── SceneTalk.html       # 界面与逻辑，单文件自包含
│   ├── server.pyw           # 本机 127.0.0.1 静态服务 + 拉起应用窗口
│   ├── README.md            # 组件详细说明
│   └── 启动SceneTalk.cmd    # 双击入口
├── .gitignore               # 模型、运行库、日志、个人数据均排除入库
└── README.md                # 本文件
```

## 隐私与数据

- 两个工具默认在本机运行：识别、翻译、历史记录、生词本和语料库都写在本机，不主动上传语音和句子。
- 在线模型的 API Key 只保存在本机（Oversub 的配置文件 / SceneTalk 浏览器 localStorage），不进仓库、不外传。
- 打包产物、模型文件、运行日志、历史记录和缓存目录全部由 `.gitignore` 排除；仓库只保留源码与文档，可执行包在 Releases 单独分发。

## 平台与现状

- 平台：Windows。
- 语种：当前仅中文 ↔ 英语双向可用；识别模型本身支持多语种，但其他方向缺少离线翻译包，软件内也不提供下载入口。设置页的翻译包表只读展示本机 Argos 包的真实安装状态，中英双向包随安装包带上。
- 许可证：仓库暂未指定开源许可证；在补充许可证之前，他人不得复制、分发或基于本项目二次发布。
