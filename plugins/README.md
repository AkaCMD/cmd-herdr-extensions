# Herdr 调度插件

`herdr-personal-dispatch/` 是 Codex / Claude 插件，独立于本仓库的 Herdr UI mods 安装器。
支持 Codex、Claude、DeepSeek 混合派发；DeepSeek 使用本机 `dsh-tui --backend dsh`，需自行配置 backend。

安装：在插件目录运行 `python install.py --host codex` 或 `python install.py --host claude`。
依赖：Python 3.10+、Herdr App/core 的 `hc`，以及所选代理 CLI；DeepSeek 还需 Node.js 与 dsh-tui。
安装后重新加载对应代理会话。任务状态、日志、凭据和真实任务材料均不进入仓库。

详细用法、验证范围和权限边界见 [插件说明](herdr-personal-dispatch/README.md)。
