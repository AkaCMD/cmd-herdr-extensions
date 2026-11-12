# Screenshot notes / 截图说明

These PNGs are direct captures of a running Herdr App window, using its built-in
**Paper** light theme. They show real conversations and native extension panels
in a separate `demo-web` project. No generated artwork or fabricated task data is
used in the published images.

这些 PNG 直接截取正在运行的 Herdr App，使用内置 **Paper** 亮色主题。
对话、任务与面板均来自独立 `demo-web` 项目的实际运行，不使用合成图像或伪造任务数据。

| Image / 图片 | Captured scene / 实际场景 |
|---|---|
| `images/todo.png` | Codex creates an HTML/CSS page and maintains a four-step plan / Codex 创建页面并维护四步计划 |
| `images/dispatch.png` | Codex controls two DeepSeek jobs for a FAQ and page checker / Codex 主控，两路 DeepSeek 分别编写 FAQ 与检查脚本 |
| `images/permissions.png` | Native permissions dialog for the demo Space / 演示空间的原生权限对话框 |
| `images/session.png` | Actual HTML/CSS changes collected from the Codex conversation / 从真实 Codex 会话采集的 HTML/CSS 改动 |
| `images/folder.png` | Native project-folder dialog for the demo Space / 演示空间的原生项目目录对话框 |

To refresh the images, run a real task in a separate demo Space, select Paper in
**Settings → Look**, select the relevant session/panel/dialog, and capture with:

更新图片时，在独立演示空间运行真实任务，选择 Paper 主题，打开相应面板或对话框后截图：

```sh
herdr-app status
herdr-app shot docs/images/todo.png
```

Use `HERDR_APP_HOME` when targeting an isolated capture window. A settings-dialog
capture shows the available controls; it does not establish live coverage of every
launcher or permission profile.

独立窗口用 `HERDR_APP_HOME` 指定。设置对话框截图仅展示实际控件，不代表所有启动器与权限档位都已逐项实测。
