# Decision Engine - AI 辅助本机安装

[English](AI_SETUP.md) | 简体中文

## 用户提示词

把本文档交给目标设备上的 AI Agent，然后只需说：

> 请按附件 `AI_SETUP.zh-CN.md` 安装 Decision Engine。

可以在同一条消息中上传当前操作系统对应的官方安装脚本。未上传时，Agent 使用本文给出的 GitHub
官方命令。

## 适用范围

本文只适用于与 Decision Engine 目标设备相同、且使用同一桌面用户运行的本机 Agent 会话。远程、
容器、云端、VM、WSL 和跨设备安装不在本文范围内。

安装脚本是环境检查、Agent 探测与选择、依赖、AQG、MCP、Skills、Hooks、激活、Doctor、更新和恢复
诊断的唯一权威。Agent 不得读取脚本内容后自行重写这些步骤。

## 给 AI Agent 的执行说明

### 1. 选择唯一安装来源

- macOS 或 Linux：`dp-install.sh`
- 原生 Windows：`dp-install.ps1`

严格采用以下优先级：

1. 当前消息明确上传了一份对应安装脚本且 Agent 可以访问时，使用该附件的准确绝对路径。
2. 没有上传对应脚本或附件不可访问时，使用第 2 节的 GitHub 官方命令。
3. 当前消息上传了多份同名安装脚本时，停止并报告 `INSTALLER_SOURCE_AMBIGUOUS`。

只有当前消息附件清单明确列出的文件才属于上传文件。不得在工作区、项目、用户目录、最近文件或本文档
目录中搜索安装脚本；不得为了安装而读取、分析或修改脚本内容。

### 2. 执行安装命令

只执行与当前系统和来源匹配的一条命令。

#### macOS

执行任何 macOS 安装命令前，必须先完成第 3 节的权限与终端能力判定。除第 3 节明确允许已知不支持的
宿主直接进入人工降级外，用户未确认“完全访问”前不得执行命令或改为让用户手动运行。

使用 Agent 宿主的普通本地命令执行器运行下列命令，**不得先使用宿主的“在可见终端中运行”功能**。
`--agent-terminal` 会自行打开并管理唯一的 Terminal.app 窗口；原 Agent 命令保持等待，安装结束后接收
完整输出和真实退出码，然后关闭安装器创建的独立终端窗口。

上传了脚本：

```bash
bash "<dp-install.sh 附件的准确绝对路径>" --agent-terminal
```

未上传脚本：

```bash
p="$(mktemp /tmp/dp-install.XXXXXX)" && curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh -o "$p" && bash "$p" --agent-terminal
```

`--agent-terminal` 只负责把原有无参数交互安装器交给 Terminal.app，不替用户作安装决定。用户直接在
该终端中完成 Agent 选择、依赖确认和其他普通提示；激活密钥只能输入原生掩码窗口。若 Agent 错误地先
在 Terminal.app 中运行该参数，安装器会提前返回 `BLOCKED`，避免产生双窗口或让 Agent 会话提前结束。

#### Linux 桌面

使用宿主提供的“在可见本机终端中运行”功能。上传了脚本时执行：

```bash
bash "<附件的准确绝对路径>/dp-install.sh"
```

未上传脚本时执行：

```bash
curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh | bash
```

#### 原生 Windows

执行任何原生 Windows 安装命令前，必须先完成第 3 节的 Windows 权限判定。用户未明确确认“完全访问”
前，不得执行命令或把命令交给用户。

使用 Agent 宿主的普通本地命令执行器运行，不得先使用宿主的“在可见终端中运行”功能。
`-AgentTerminal` 会打开一个可见 Windows PowerShell 窗口，在其中运行原有无参数交互安装器，等待真实
退出码，并让该子窗口在安装器退出后自动关闭。

上传了脚本时执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<dp-install.ps1 附件的准确绝对路径>" -AgentTerminal
```

未上传脚本时执行：

```powershell
$p="$env:TEMP\dp-install.ps1"; irm "https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.ps1" | Set-Content -LiteralPath $p -Encoding UTF8; powershell.exe -NoProfile -ExecutionPolicy Bypass -File $p -AgentTerminal
```

用户在可见子窗口中完成 Agent 选择和普通提示；激活 secret 仍只进入原生掩码窗口。Agent 命令必须持续
等待并取得子进程数字退出码，不得把“窗口已打开”误报为“安装成功”。

除准确文档化的入口参数外，不得添加其他参数、环境变量覆盖、包装器、重定向或对整个安装器使用
`sudo`。不得在 Agent 的后台沙箱 Shell 中重现交互流程，也不得替用户猜测输入。

### 3. 权限与交互

提示词和安装脚本都不能替用户切换宿主的“询问审批 / 自动审批 / 完全访问”等安全模式，也不能在安装
后自动切回。一次性的 macOS“允许控制终端”只授予 Automation 权限，不等于宿主的“完全访问”。

macOS 权限与终端交接必须严格按以下状态机执行，不得自行回到前一步：

1. 当前宿主是 WorkBuddy 或 WorkBuddy AI 时，无论版本号和当前权限模式，直接报告
   `VISIBLE_TERMINAL_HANDOFF_UNSUPPORTED` 并进入下述人工终端降级。不得要求切换完全访问，也不得执行
   `--agent-terminal` 或 `--agent-activate`。这是当前已验证的宿主能力限制，不是安装器错误。
2. 其他 Agent 在执行安装或继续激活前，必须明确告诉用户临时切换为“完全访问”，结束后再手动恢复。
   用户尚未确认时，报告 `AGENT_FULL_ACCESS_REQUIRED` 并停止，不得提前人工降级。
3. 用户在当前会话回复“已开启完全访问”、"Full Access enabled" 或同等明确确认后，将该确认视为当前
   安装任务持续有效，只执行原自动交接命令一次。不得再次询问、要求“确认已对当前会话生效”或让用户
   重复同一句确认。
4. 自动命令返回退出码 `3`，输出包含 `dp-install: BLOCKED`、`did not permit`、Terminal.app 未启动、
   启动超时、宿主沙箱拒绝或权限错误 `-10004` 时，立即报告 `VISIBLE_TERMINAL_HANDOFF_BLOCKED`，并在
   **同一条回复**中给出下述唯一人工命令。不得再次请求权限确认或先问用户是否需要命令。

一次性的 macOS“允许控制终端”不能替代完全访问确认。其他 Agent 不得仅因宿主名称或一次未授权失败
而提前降级；但用户已经确认完全访问后，不得把交接失败重新解释为“尚未确认权限”。

人工终端降级仅适用于终端交接失败。若 Terminal.app 已成功打开，后续依赖、网络、激活、Doctor 或
安装器错误都不是交接失败，必须保留并原样报告，不得人工重跑。

首次安装且上传了脚本时，让用户在 Terminal.app 中执行：

```bash
bash "<dp-install.sh 附件的准确绝对路径>"
```

首次安装且未上传脚本时，让用户执行：

```bash
curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh | bash
```

继续激活时使用第 4 节的人工 `--activate` 命令。进入降级的同一条回复必须包含与当前来源匹配的一条
命令及“请在 Terminal.app 中执行，完成后贴回数字退出码和最后一行 `dp-install:`”；不得只报告失败、
再次索取权限确认或询问用户是否需要命令。不得读取脚本后重写安装步骤，收到结果前不得宣称成功。

不得要求 macOS 完全磁盘访问，不得修改操作系统安全设置或循环重试。自动流程成功、失败或转为人工
降级后，如果用户曾切换权限，要提醒用户手动恢复原权限模式。

原生 Windows 使用 Agent 宿主自身的“完全访问”模式，不是 macOS“完全磁盘访问”。执行安装或继续激活
前，必须明确告诉用户临时开启“完全访问”，完成后再手动恢复原权限模式。用户尚未在当前会话明确确认
时，报告 `AGENT_FULL_ACCESS_REQUIRED` 并停止，不得执行命令或提前人工降级。一次明确确认对当前安装
任务持续有效，不得再次询问。

确认后只自动尝试一次文档化的 `-AgentTerminal` 或 `-AgentActivate` 交接。若返回退出码 `3`、报告
`dp-install: BLOCKED` 或没有打开可见 PowerShell 窗口，立即报告
`VISIBLE_TERMINAL_HANDOFF_BLOCKED`，并在同一条回复中给出唯一适用的人工命令。首次安装使用第 2 节
同来源命令并移除 `-AgentTerminal`；继续激活使用下述同来源 `-Activate` 命令。不得在给出命令前再次
要求确认。窗口已经打开后，后续安装、网络、激活或 Doctor 错误属于产品结果，不得触发自动重跑。
自动流程结束或降级后，提醒用户手动恢复 Agent 原权限模式。

用户在可见终端中直接处理所有普通提示。Agent 不得向聊天索取激活密钥、endpoint、设备 token 或其他
secret；这些值只能进入安装器的原生掩码窗口。

### 4. 稍后继续激活

如果安装器报告 `activation=pending`，用户随后要求继续或重试激活时，不得重跑第 2 节的完整安装流程。
只执行与原系统和来源匹配的一条仅激活命令。

macOS 上传了脚本：

```bash
bash "<dp-install.sh 附件的准确绝对路径>" --agent-activate
```

macOS 未上传脚本：

```bash
p="$(mktemp /tmp/dp-install.XXXXXX)" && curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh -o "$p" && bash "$p" --agent-activate
```

在 macOS 上仍通过宿主的普通本地命令执行器运行，不得先使用“在可见终端中运行”功能。该入口只打开
一个受管 Terminal.app 窗口并启动原生掩码激活，不执行更新检查、Agent 选择、AQG 安装或 MCP/Skills
重写。

如果已按第 3 节进入人工终端降级，上传了脚本时让用户在 Terminal.app 中执行：

```bash
bash "<dp-install.sh 附件的准确绝对路径>" --activate
```

未上传脚本时让用户执行：

```bash
p="$(mktemp /tmp/dp-install.XXXXXX)" && curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh -o "$p" && bash "$p" --activate
```

`--activate` 仅供用户已经位于可见终端时使用；不得把 `--agent-activate` 交给用户手动执行。

Linux 桌面上传了脚本：

```bash
bash "<dp-install.sh 附件的准确绝对路径>" --agent-activate
```

Linux 桌面未上传脚本：

```bash
p="$(mktemp /tmp/dp-install.XXXXXX)" && curl -fsSL https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.sh -o "$p" && bash "$p" --agent-activate
```

在 Linux 上通过宿主提供的可见本机终端运行。仅激活入口只接受完整、干净且状态可验证的受管安装；
安装缺失、损坏或存在恢复标记时必须停止并原样报告，不得退回完整安装或自行修复。

原生 Windows 上传了脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<dp-install.ps1 附件的准确绝对路径>" -AgentActivate
```

原生 Windows 未上传脚本：

```powershell
$p="$env:TEMP\dp-install.ps1"; irm "https://raw.githubusercontent.com/deeppatternai/decision-engine/main/dp-install.ps1" | Set-Content -LiteralPath $p -Encoding UTF8; powershell.exe -NoProfile -ExecutionPolicy Bypass -File $p -AgentActivate
```

以上命令通过 Agent 宿主的普通本地命令执行器运行，只打开一个可见 PowerShell 窗口并执行仅激活流程；
不检查更新、不选择 Agent、不安装 AQG，也不改写 MCP/Skills。若 Windows 自动交接被阻止，移除
`-Agent`，把同来源 `-Activate` 命令交给用户在已经可见的 PowerShell 终端中执行。不得把
`-AgentActivate` 交给用户手动运行。

不得直接运行 `python3 -m installer.permanent_setup`，也不得自行拼接 Python、工作目录或 `PYTHONPATH`；
这类内部模块命令依赖受管源码目录，容易产生 `ModuleNotFoundError` 并绕过顶层安装器的恢复检查。

只有确实出现原生掩码激活窗口时，才可报告“激活窗口已打开”。命令非零退出、出现
`ModuleNotFoundError` 或没有窗口时，必须如实报告失败，不得根据意图宣称成功。

### 5. 报告结果

安装进程结束后报告：

- 平台和来源（`uploaded` 或 `official-github`）；
- 实际执行的完整命令；
- 数字退出码；
- 最后一行 `dp-install:` 状态；
- 所有 `ERROR`、`BLOCKED` 或 `PARTIAL` 结果；
- 安装器要求的重启、信任、授权或其他用户操作。

退出码 `0` 表示成功；退出码 `4` 是部分完成，不属于完整成功；其他非零退出码表示失败。需要重启 Agent
时，必须完全退出系统托盘或后台进程后重新打开，只新建聊天不算重启。

失败时保留完整输出。不得自行删除或重命名 `.deeppattern`、重置 checkout、修改 MCP/Skills 配置、
安装替代 Python，或编造恢复步骤。
