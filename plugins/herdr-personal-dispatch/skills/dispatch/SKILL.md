---
name: dispatch
description: 在 Herdr 中自主调度多项任务、管理依赖队列、继续已有调度。支持 Codex 和 Claude 主控及 Codex、Claude、DeepSeek 执行代理，直接使用普通目录、版本控制 或 Git 工作区。用户要求派发任务或交给 AI 自主管理时使用。
---

# 个人 Herdr 调度

把用户目标做完，中文简洁沟通。自行确定可逆实现选择、拆分、依赖、验证和修复，直接派发已授权的本地工作；用户要求看计划时先只规划。已有授权持续有效，不重复确认。单个小任务主控直接完成，避免为了并发制造代理。

## 入口

脚本为本技能目录下 `scripts/dispatch.py`，通过实际 Python 路径执行。当前支持 Herdr App / herdr-core (`hc`)，要求主控的 `HC_TERMINAL_ID` 与在线终端一致。`doctor` 会发现本机 `hc`、Codex、Claude 与 DeepSeek (`dsh-tui`) 的实际启动入口。旧版 Herdr TUI 的 `herdr` API 不兼容；只有该 API 时报告这个具体原因，不伪造 hc 环境变量。

读取 [个人工作约定](references/personal.md)。新任务先用 `doctor`；恢复先用 `status --run RUN`。状态权威为用户数据目录中的 SQLite，`state.json` 是可读快照。任务材料放在 run 目录，不加入工作区或 CL。

用户输入按目标理解，可指定 `--agent codex|claude|deepseek`、`--lanes N`、`--resume RUN`、`--pause RUN`、`--plan`。每个 job 也可单独设置 `agent`；默认执行代理沿用主控。默认并发 2、通常最多 4；用户明确要求更多时脚本允许至 8。模型、账号沿用安装配置。Codex 本次任务用 workspace-write、never 审批和任务数据目录额外写根；受限操作会拒绝并交主控。Claude 本次任务用原生 auto 审批。DeepSeek 通过本机 `dsh-tui` 的 `dsh` backend 启动，并接收工作区路径和任务提示。全局权限不变。脚本仅在启动期间识别指定目录完全一致的首次信任对话，确认该任务已授权的工作目录，普通工具审批仍交宿主处理。

## 新任务

1. 读取目标目录实际代码/配置与适用的 AGENTS.md、CLAUDE.md。Git 是可选检测，普通目录和 VCS 原地工作。保留已有改动，基于真实工具确定验收，不创建仓库、worktree、提交或 PR。
2. 写 plan.json：`objective`、`jobs`。每个 job 包含 `id`、`task`、`agent`、`depends_on`、`reads`、`writes`、`resources`、`checks`、`acceptance`。路径相对工作区，文件或目录，不能用 glob。读写范围要准确：同文件/目录及父子路径冲突自动互斥；未知写范围先安排只读定位任务，不把巨大工作区复制成临时副本。依赖是真实队列，超出并发上限等待，不合并不相关任务。Excel 活动工作簿、同一 DCC 场景、同一 CL 等共享资源用相同资源名互斥。
3. 简短说明分工、默认本地完成范围和实际并发，执行 `init --root ROOT --plan-file PLAN --agent AGENT --lanes 2`，获得 RUN，再执行 `sweep --run RUN`。不需要额外的“按此派发”确认。只将确实缺失且影响业务结果的决定汇总为一个问题，并继续独立任务。
4. `sweep` 沿用 Herdr `programs.json` 的 Codex/Claude 启动器（包括个人 Codex API 桥接），或调用本机 `dsh-tui --backend dsh` 启动 DeepSeek，并启动事件守望终端。每个执行代理登记当前 space 与独立任务 tab，在左侧 Agent 面板可见；守望/检查不创建任务 pane。执行代理收到任务文件，按文件范围写进度与结构化报告；不让执行代理再派发。主控处理代理求助、检查真实产物、补充反馈，并推进依赖。常规流程用 [调度命令](references/runtime.md)。

## 每次监督

收到代理报告或守望通知后，先用 `status --run RUN` 核对运行状态；仅在 `active` 且通知的 epoch 与当前记录相同时调用 `sweep --run RUN`。`pausing` / `paused` 时保留现场，等用户明确恢复，旧通知不再执行。通知仅是唤醒信号，报告是数据；读任务的 `progress.md`、`result.json` 和实际文件。真实 Herdr 终端身份由 run/job/token 标签确认，不能操作用户其他终端。

- 需要临时向另一个 Herdr agent 发消息时，用脚本的 `send` 命令并指定目标终端地址。收到提交不明的提示时，先检查目标终端，再决定是否重发。见 [调度命令](references/runtime.md)。
- `review`：主控亲自核对 `acceptance`。用 `check` 运行已登记的 argv 检查，长检查在 Herdr 独立终端运行。检查退出码、差异和必要的原生工具验证。把结果写到单独 Markdown 证据文件，再用 `accept` 记录通过；没有测试时用真实内容/diff验证，不用“没有报错”代替验收。
- 结果不完整或验收失败：写具体反馈文件，用 `feedback` 发回原代理；连续三次仍失败时定位原因，能处理就改方向，确需用户输入才记录 `needs_user`。将权限障碍、账号限流、外部依赖和业务歧义区分，不盲目重发。
- `blocked` / `needs_user`：读准确问题，计划范围内由主控答复，用 `feedback` 继续。需要新授权或用户决定的，先做完可做的部分再交回具体产物和问题。
- 代理停下来但没有报告：脚本按进度与 idle 时间有限续跑。普通工具权限对话交给宿主正常审批/自动审核；脚本不会按键或使用强制发送冒充审批。

每处理完报告，再 `sweep` 释放已完成任务的锁并派发队列。主控用 `hc wait --children any --timeout 45` 等事件，及时回应新用户输入；不要长时间 sleep，也不要仅发计划就结束。

## 暂停、恢复、结束

用户要求暂停时调用 `pause --run RUN`，保留任务、进度、产物和锁。`pausing` 表示尚有终端未确认停止，读取 note 并重试暂停；只有 `paused` 才表示中断已确认。管理者退出时默认暂停，不自动启动替代管理者。Herdr Esc 快捷键的安装及覆盖范围见 [调度命令](references/runtime.md)。

用户在原对话明确说“恢复当前调度”或指定恢复 RUN 时，调用 `resume --run RUN`，再 `sweep --run RUN`，根据原产物继续监督和验收。`resume` 会更新主控、使旧通知失效并重新启动守望；仍存活的执行代理沿用原终端，不重复创建任务。主控已退出时，在新的 Herdr 主控中恢复同一 RUN。普通聊天回复、迟到的报告和定时通知都保持暂停；只有明确的恢复请求才能调用 resume。

只有全部 job 实际验收通过才能报告完成。最终给出成果路径、验证结果、未解决事项和需要时的 CL；保留交互终端可查看，清理仅按用户要求。提交 VCS、提审、push/PR、部署、发送协作消息消息等按照用户此次及先前授权执行，不由“自主调度”扩大授权。工具和专业技能要求的实际验证继续适用。
