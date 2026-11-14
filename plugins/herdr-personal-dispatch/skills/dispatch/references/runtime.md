# 调度命令

以本技能中的 `scripts/dispatch.py` 为 SCRIPT。Python 3.10+，标准库即可。

```powershell
python SCRIPT doctor
python SCRIPT init --root 'D:\Workspace\project' --plan-file PLAN --agent codex --lanes 2
python SCRIPT sweep --run RUN
python SCRIPT status --run RUN
python SCRIPT check --run RUN --job JOB
python SCRIPT accept --run RUN --job JOB --evidence-file EVIDENCE
python SCRIPT feedback --run RUN --job JOB --text-file FEEDBACK
python SCRIPT send --to t7 --text '请查看最新检查结果'
python SCRIPT send --to t7 --text-file MESSAGE
python SCRIPT pause --run RUN
python SCRIPT resume --run RUN
```

`resume` 会重新启动守望程序，再执行 `sweep` 继续原队列。原管理者会话仍在时可直接说“恢复当前调度”；管理者已退出时，在新 Herdr 主控中指定同一 RUN 恢复。仍存活的执行代理沿用原终端。

`pausing` 表示停止尚未全部确认，note 列出具体阻碍，重试 `pause` 或面板的“暂停调度”；此时不能恢复。`paused` 保留锁、进度和产物，等待显式恢复。run 的 epoch 在暂停和恢复时递增；旧通知与补提交操作失效。

`send` 可向 Herdr 中的 Codex、Claude Code 或 DeepSeek 终端发消息。短消息用 `--text`。长消息可保存在文件中，用 `--text-file` 通知同一台机器上的代理读取该文件。消息还完整留在接收端输入框时，脚本会核对终端身份和状态，再补一次提交键。

`check` 在单独 Herdr 终端实际执行 plan 的 checks，结果写入 `checks.json`，主控读完再 accept。`hc wait` 可能因收到消息提前返回；用 `hc get` 确认检查终端已退出，并检查当前 attempt 的 checks.json，不能把 wait 返回当作检查成功。checks 是 argv 数组，默认在工作区运行，可指定工作区内 cwd 和 timeout；不使用 shell 拼接。`accept` 必须有非空验收证据、匹配当前 attempt 的 done 报告和成功检查结果；accepted 后输出文件的 hash 进入状态。证据由主控写，执行代理无权自行 accept。

```json
{
  "objective": "分析配置并修复指定问题，完成本地验证",
  "jobs": [
    {
      "id": "inspect",
      "task": "只读定位目标配置与影响，写分析到 run 目录供 fix 使用",
      "agent": "codex",
      "depends_on": [],
      "reads": ["Resource"],
      "writes": [],
      "resources": [],
      "checks": [],
      "acceptance": ["报告列出实际文件与错误证据"]
    },
    {
      "id": "fix",
      "task": "根据 inspect 的报告修复已定位问题，保留其他改动",
      "agent": "claude",
      "depends_on": ["inspect"],
      "reads": ["Resource/Config/example.json"],
      "writes": ["Resource/Config/example.json"],
      "resources": [],
      "checks": [{"argv": ["python", "validate.py"], "timeout": 60}],
      "acceptance": ["业务差异符合目标", "实际验证命令通过"]
    }
  ]
}
```

路径为精确文件或目录，父子冲突；同名 resources 排他。目录 scope 不是完整备份，执行代理确定文件后先运行 `backup --run RUN --job JOB --file PATH`（可重复 --file），再修改。该命令只允许本 job 写范围内的文件；备份在 run 目录，保留原字节与 SHA256。脚本不自动恢复覆盖源文件。

默认数据目录 `~/.local/share/herdr-personal-dispatch`；可用 `PERSONAL_DISPATCH_HOME` 指定隔离目录。`--run` 可用 run ID 或它的绝对路径。单任务 `request`、`progress`、`result`、`checks`、验收证据及日志在 `runs/RUN/tasks/JOB`；SQLite 为权威，JSON/Markdown 便于审阅。

守望程序在有 run 标签的 Herdr 终端中监听 `hc events`，并按间隔发监督提醒；它不代替 AI 做验收或回答业务问题。管理者退出或 `dispatch.controller` 标签不匹配时暂停本 run，保留原因，不自动接管。管理者正在工作时不向其追加监督消息，避免形成待执行的继续消息队列。Codex 本次运行限定为工作区与任务数据目录写入，权限拒绝不弹无限审批；Claude 使用原生 auto 模式。自动化不保证消除工具审批，真正的拒绝会交给主控。

执行代理在主控或其可见父终端的 Herdr space 中各占一个任务 tab。主控与父终端都没有 space 时，Herdr 会按 run 创建一个独立 space。代理报告状态后可在左侧 Agents 面板看到。

旧版 Herdr TUI 无 hc 时 doctor 报告不兼容，不自动下载安装、不启动另一个 Herdr 服务。恢复在原来的 Herdr scope/机器中进行；本版只调度本机目录。

## Herdr Esc 暂停

仓库安装器 `personal-dispatch` 同时复制 `mods/dispatch-control`。在个人布局中启用后，扩展只接管 Esc，没有独立按钮或控制条。聚焦管理者时，Esc 先提交持久暂停，再转发停止键并中断本 run 的活动任务和检查。聚焦普通终端或执行代理时，Esc 通过 `hc keys` 转发，不暂停整个 run；该转发遵守 core 的输入串行与安静期规则。Ctrl+C 保持原有绑定。

```powershell
python scripts/install.py personal-dispatch
python scripts/install.py personal-dispatch --apply
python "$herdrHome/mods/dispatch-control/enable.py" agent-progress
python "$herdrHome/mods/dispatch-control/enable.py" agent-progress --apply
herdr-app check "$herdrHome/mods/dispatch-control"
```

`herdrHome` 按仓库 README 的安装例子设置。已有文件不同先核对，再按 README 使用带备份的替换流程。启用器为布局和 keys.json 留备份，保留其他快捷键；已有 Esc 绑定冲突时拒绝覆盖。快捷键只在指定布局生效，由零高度的常驻 mod 接收命令。恢复时在原对话明确说“恢复当前调度”，管理者执行 resume 和 sweep，重启守望并继续管理原会话。

此功能只使用扩展和现有 hc 命令。本机扩展不控制远程管理者；远程会话需在所在机器安装。未启用此扩展的布局、代理在终端内自己绘制的鼠标停止按钮、从其他客户端发送的中断，不会经过此暂停入口。现有 core 无法撤销已经提交到代理内部的消息；扩展会作废旧发送版本、在中断前等待本扩展发送操作结束，并在暂停期间禁止补按 Enter，但不承诺清空代理内部队列。需要暂停调度时，在已启用扩展的管理者终端按 Esc，或明确说“暂停当前调度”。
