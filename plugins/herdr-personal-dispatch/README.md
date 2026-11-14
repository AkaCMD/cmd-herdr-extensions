# 个人 Herdr 自主调度

为 user 的 Windows、项目/版本控制 和协作消息工作方式设计。Codex 与 Claude Code 可以作为主控；Codex、Claude Code 和 DeepSeek 可以混合执行任务。**使用 Herdr App / herdr-core 的 `hc` 接口**，直接在普通目录、VCS 工作区或 Git 目录工作；不要求 Git、worktree、提交或 PR。

说清目标后，主控自行规划和派发。Codex/Claude 沿用 Herdr `programs.json` 中的启动器；DeepSeek 任务由本机 `dsh-tui --backend dsh` 启动。各客户端的凭据仍由各自配置处理，调度插件不读取或保存凭据。执行任务保持为 Herdr 中的交互终端，在当前 space 的独立任务 tab 与左侧 Agent 面板显示，可查看、继续对话；守望/检查不创建任务 pane。默认同时执行 2 路，支持依赖队列、跨 run 读写范围互斥、共享资源互斥、原字节备份、有限续跑、主控验收和暂停恢复。并发只计算实际启动/运行的任务，待验收或受阻任务仍保留文件锁，其他独立任务可以继续。

## 使用

安装后新开一个 Herdr 中的 Codex 或 Claude 会话。Codex 可使用 `$dispatch`，Claude 使用 `/herdr-personal-dispatch:dispatch`，也可自然语言要求“在 Herdr 中自主派发这些任务”；在任务描述中可指定使用 DeepSeek 执行某个 job。

```text
$dispatch 分析这个普通目录里的关卡配置，修复已经确认的问题，完成本地验证；使用 Codex，最多两路。
```

```text
/herdr-personal-dispatch:dispatch 让 Codex 定位配置问题，Claude 修复脚本；你负责监督和验收，先完成本地结果。
```

暂停说“暂停当前调度”；继续说“恢复当前调度”或“恢复 RUN”。可安装 [Herdr Esc 暂停](skills/dispatch/references/runtime.md#herdr-esc-暂停)，让管理者终端中的 Esc 暂停整组任务。主控执行对应脚本命令，用户不用手写 plan.json。新增或恢复的主控必须在 Herdr 中。一个主控一次管理一个 run，不同主控的 run 共用本机 SQLite 文件锁。

## 安装

本目录本身是本地 marketplace 和双客户端插件；执行安装器注册到两个客户端：

```powershell
python .\install.py
```

只安装一个客户端：`python .\install.py --host codex` 或 `--host claude`。安装器发现原生 CLI，调用官方 plugin marketplace/add/install 命令，不直接重写模型、权限或 MCP 配置。若只有旧 Herdr TUI 的 `herdr`，本版本的 `doctor` 会明确提示接口不兼容，不自动安装另一套 Herdr。

## 验证与边界

Python 3.10+ 标准库；需要在线 `hc` 和所选代理 CLI。DeepSeek 执行任务需要已安装的 `dsh-tui` 及其 `dsh` backend 配置；`doctor` 检查启动器版本。脚本 `doctor` 也检查 Herdr 主控环境。执行代理按 writes 修改，登记备份，报告 done；主控核对真实产物和验收，实际检查命令通过后才能 accept。XLSX 仍需原生 Excel 打开和项目导表验证，CSV 保持编码，VCS 保留无关 opened 文件。范围互斥依赖准确的计划和代理遵守文件范围，不能当作操作系统沙箱。

守望程序是 Herdr 中的独立终端，监听 `hc events` 并向空闲主控发送监督通知，不依赖 Claude 的 loop skill。它和执行代理使用 `--outlive-parent`；管理者终端退出时默认暂停任务，不自动重建管理者。暂停保留任务与锁，恢复会重启守望并复用仍存活的执行会话。机器关机或 Herdr 服务停止后，需要在 Herdr 中恢复同一 RUN；断开窗口与停止服务不同。

常规本地工作不强制再次确认计划；用户已授权的发布、提审等可由主控继续执行。Codex 每次运行使用 workspace-write 沙箱、never 审批与任务目录额外写根；Claude 使用原生 auto 模式。只确认本次已授权工作目录的首次信任提示，普通工具审批不会被脚本按键绕过。调度本身不会扩大对 VCS submit、代码评审、push/PR、部署或协作消息消息的授权，也不会更改全局审批。代理缺少原生工具或权限时，主控处理或给出具体阻碍。

状态默认在 `~/.local/share/herdr-personal-dispatch`，任务数据不放进 项目 源目录、Git 或 CL。`PERSONAL_DISPATCH_HOME` 可指定隔离位置；子终端会收到显式数据目录参数。详细规则见 [SKILL.md](skills/dispatch/SKILL.md) 和 [调度命令](skills/dispatch/references/runtime.md)。

## 开发验证

```powershell
python -m unittest discover -s tests -v
python skills/dispatch/scripts/dispatch.py doctor
node tests/test_controls.mjs
# 仅在能创建隔离 core 的环境运行；合成代理，不调用模型服务
python tests/pause_smoke.py
python tests/controls_smoke.py
```

安装规范参考 [OpenAI 插件文档](https://developers.openai.com/plugins/build/plugins)，运行接口以本机 `hc --skill`、`hc schema`、`hc run --help` 为准。

自动化验证：32 项 Python 调度与启用器测试、界面命令逻辑测试通过，覆盖暂停后不唤醒、停止失败保留 pausing、恢复重启守望并复用代理、暂停与恢复并发。`herdr-app check mods/dispatch-control` 通过。`tests/pause_smoke.py` 在隔离 core 的真实终端中验证暂停、恢复和管理者退出；`tests/controls_smoke.py` 在独立窗口验证 零高度 Esc 扩展的 mod 命令、显式恢复通知、新守望及原执行会话复用，并检查窗口无错误与截图。两项烟雾测试使用合成代理，不调用模型服务；尚未测试物理键盘按键或真实模型会话的中断。

已有执行代理启动验证：实际 Herdr 桥接启动 Codex 和 Claude，在无 Git 临时目录创建文件、登记备份、独立检查与主控验收通过；窗口截图确认两个执行代理显示在左侧 Agent 树中。真实 XLSX/VCS/协作消息业务操作及意外断电恢复没有在本轮试验，仍按专业技能验证。
