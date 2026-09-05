# Decision Engine - 自动安装与更新（把这份文档交给 AI Agent 执行）

[English](AI_SETUP.md) | 简体中文

> **怎么用**：把这份完整文档交给任一受支持宿主，或其他可以操作本机终端的 AI Agent，
> 然后说：“请按这份文档安装 Decision Engine。”Agent 会安装或修复 AQG、运行 AQG Doctor、完成 AQG
> 自己的 standalone 配置；安装不含凭据、支持 DE Lite 的 `decision-engine` MCP 传输入口和 skills，
> owner 值齐全时再激活设备并完成验证；不要手工修改 MCP 配置。
>
> Agent 会检查环境、取得可信安装源码、完成 AQG standalone 流程、安装 Decision Engine，接入可供
> DE Lite 使用的 MCP 传输入口并运行 Doctor；永久激活后才开放托管能力。随后 Decision Engine 会在
> Agent 启动时自动检查并应用已签名的 `stable` 版本。
>
> 本文只面向**第一次安装**。如果 `~/.deeppattern/decision-engine` 已经存在，立即停止并报告，保留
> 原路径；历史迁移和恢复不在本文范围内。

---

## 安装前需要准备

必须具备：

- Python 3.12 或更高版本；
- Git 2.45 或更高版本；
- 至少一个下表中的注册目标客户端；
- 当前私仓阶段，用户自己的 GitHub 账号需要拥有仓库读取权限，并且普通 Git 认证已经可用。

安装器接受下列准确客户端 ID 和路径：

| 宿主 | `--client` ID | 用户级 MCP 配置 | 受管 Skills | 平台范围 |
|---|---|---|---|---|
| Claude Code | `claude-code` | `~/.claude.json` | `~/.claude/skills` | 受支持平台 |
| Claude Desktop | `claude-desktop` | Windows：`%APPDATA%/Claude/claude_desktop_config.json`<br>macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`<br>Linux：`~/.config/Claude/claude_desktop_config.json` | 无 | 受支持平台 |
| Codex | `codex` | `~/.codex/config.toml` | `~/.codex/skills` | 可加载全局 Skills 的版本及其受支持平台 |
| Cursor | `cursor` | `~/.cursor/mcp.json` | `~/.cursor/skills` | 受支持平台 |
| 阿里 Qoder Desktop | `qoder` | `~/.qoder/mcp.json`（Windows）；`~/.qoder/settings.json`（macOS） | `~/.qoder/skills` | Windows Desktop 1.106.3+；macOS Qoder.app 0.1.3+ |
| 阿里 Qoder CN Desktop | `qoder-cn` | `~/.qoder-cn/settings.json` | `~/.qoder-cn/skills` | 仅 macOS Qoder CN.app 0.1.4；Qoder CN IDE 是另一个未支持产品 |
| TRAE Desktop | `trae` | `~/Library/Application Support/Trae/User/mcp.json` | `~/.trae/skills` | macOS Trae.app 3.5.81 |
| TRAE Work | `trae-work` | Windows：`%APPDATA%/TRAE SOLO/User/mcp.json`<br>macOS：`~/Library/Application Support/TRAE SOLO/User/mcp.json` | `~/.trae/skills` | Windows and macOS Desktop 0.1.48+ |
| TRAE CN Desktop | `trae-cn` | `~/Library/Application Support/Trae CN/User/mcp.json` | `~/.trae-cn/skills` | macOS Trae CN.app 3.3.95 |
| TRAE Work CN | `trae-work-cn` | Windows：`%APPDATA%/TRAE SOLO CN/User/mcp.json`<br>macOS：`~/Library/Application Support/TRAE SOLO CN/User/mcp.json` | `~/.trae-cn/skills` | Windows and macOS Desktop 0.1.48+ |
| 腾讯 WorkBuddy Desktop | `workbuddy` | `~/.workbuddy/mcp.json` | `~/.workbuddy/skills` | Windows and macOS Desktop |

Graphic Explanation 弹窗右侧追问通过 API 提供，不需要 host CLI。支持的宿主仍可使用公共的
Graphic Explanation / Discussion Board 原生窗口及审计 Stop Panel。

Decision Engine 由 owner 托管。审计、画图、讨论板等托管功能需要服务 owner 下发两个值：

- `DE_ENDPOINT`：owner 提供的 HTTPS 服务地址；
- `DE_ACTIVATION_SECRET`：owner 提供的设备激活密钥。

这两个值**不是核心安装的前提**。安装器可以在激活前注册不含凭据的本地 MCP 传输入口，让 DE Lite
可达；永久激活成功前，托管能力仍不可用。绝不要要求用户把真实值粘贴进聊天、命令行或普通文件。

如果两个值已经存在于 Agent 当前进程环境中，安装器会直接采用，不弹窗，也不会把 secret 放进命令
参数。激活成功后只永久保存 endpoint 和服务端签发的设备凭据，不保存原始 activation secret。如果
缺少任意一个值，仍必须先安装或修复 AQG、完成 AQG standalone 配置，并在 AQG Doctor 通过后，才可
安装 Decision Engine 核心。此时宿主中可能已有本地 MCP 条目，但它不含 endpoint 或凭据，只开放允许
的 DE Lite 路径。以后拿到值时，由 Agent 从第 5 步继续，不需要重装。

Windows 通过 PowerShell 写入的 User 级环境变量只会被后来启动的新进程继承。旧 Agent 进程可能读
不到，但这不会阻止安装；装完后仍可通过第 5 步完成永久配置。

---

## 以下内容由 AI Agent 执行

> AI：逐步执行并验证。clone、运行安装代码或写 Agent 配置之前，先简短说明要做什么。
> 用户要求按本文安装，表示已经授权下面的正常安装步骤。
>
> 核心安装、Git 认证、签名验证、固定路径或 MCP 接线失败时必须停止并如实报告。GUI 可选依赖或设备
> 激活失败也要报告，但不得回滚已经完成的核心安装。
>
> 不得打印、记录、提交真实 endpoint、激活密钥、设备 token、Git 凭据或完整配置文件，也不得把
> 它们复制进自己的上下文。绝不要让用户在聊天中发送 secret。
>
> 安装驱动 checkout 脏、分叉或状态异常时，不阻止整个安装：保留原目录，换一个新目录 clone 干净
> 副本。固定安装目录已存在或类型异常时，则必须停止本文流程并报告。两个目录都不得 reset、stash、
> clean、删除、移动或覆盖。

### 0. 检查环境和目标客户端

如果 owner 值已经存在于 Agent 进程中，应把它们当作受限输入。除了第 3-4 步的安装命令和第 5 步
明确的 `permanent_setup --from-env` 命令，其他所有子进程都必须移除这两个值。所以下面的 Bash 命令
使用 `env -u`。清理子进程环境时不得读取或复制真实值。

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git --version
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 --version
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 -c "import sys, ssl, venv; print(sys.executable)"
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 -m pip --version
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 -c "import tkinter; print('tkinter: OK')"
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET python3 -c "import webview; print('pywebview: OK')" || echo "pywebview: not installed yet (optional)"
```

- 不要把 `PATH` 中第一个 `python3` 当成整台电脑的唯一检查结果。请枚举所有
  `python3.*`、`python3` 和 `python` 候选（例如 `type -a python3 python3.12 python`），并对每个候选运行版本和模块探针。
  macOS 可能把 Apple 的 Python 3.9 放在兼容的 Homebrew 或用户管理的 Python 前面；第一个候选失败不等于必须安装
  Python。选择第一个通过全部必需探针的候选，记录准确绝对路径，并在继续前把 `DE_PYTHON` 设为该路径。
- Python 3.12 是硬性最低版本。Agent 必须探测解释器、记录准确绝对路径，并验证版本以及 `ssl`、`venv`、`pip`、
  `tkinter`。复用现有的 Python 3.12 或更高版本，只要这些探针全部通过；更高版本同样有效，不得因此要求安装或
  降级，也不要弹出版本选择提示，直接报告所选版本并继续。如果没有 Python、版本低于 3.12 或缺少必需模块，应说明
  检测结果并询问：“是否允许我安装并验证满足 3.12+ 要求的最新稳定 Python 3.x，然后继续？”用户授权后，
  Windows/macOS 使用当前 python.org 安装器或用户认可的包管理器；Linux 使用发行版包管理器，安装满足 3.12+ 要求
  的最新稳定 Python 3.x 及匹配的 Tk 包（通常叫 `python3-tk`，但应以检测到的发行版包名为准）。验证新解释器后再继续。
  由用户确认系统提权或安装界面，再重新验证全部项目；用户拒绝则停止。本要求同时适用于原生 Windows 和 macOS/Linux。
- 在 macOS/Linux/WSL 和原生 Windows Git Bash 中，无论解释器原本就存在还是刚安装，都必须为后续
  命令绑定已经验证的解释器。把占位符替换为验证命令打印的准确路径：

  ```bash
  export DE_PYTHON="<第 0 步打印的 Python 绝对路径>"
  "$DE_PYTHON" -c "import sys, ssl, venv, tkinter; print(sys.executable)"
  ```

  原生 Windows PowerShell 使用第 5 步所示的等价 `$PythonPath` 变量。
- Git 必须为 2.45+。Git 版本过低时，停止并让用户先升级。
- `tkinter` 用于第 5 步的掩码配置窗口。缺少时不得改用聊天或命令行收集 secret。即使以后才提供
  owner 值，Python/Tk 就绪仍是本安装流程的前置条件。
- `pywebview` 用于画板、图解、漫画等可视化窗口。缺少包不代表安装失败：安装阶段会在 MCP 使用的
  Python 中主动尝试安装，首次打开可视化窗口还会重试；Doctor 会区分“包缺失”和“系统 GUI backend
  不可用”。
- 原生 Windows 客户端必须使用 Git for Windows 自带的 Git Bash 运行 `install.sh`，不要自行翻译成
  PowerShell。只有本次选择的全部客户端都运行在同一个 WSL 发行版中时，才可使用 WSL；不要在 WSL
  中给原生 Windows Codex、Claude Code、Claude Desktop、Cursor、TRAE Work、TRAE Work CN、
  WorkBuddy 或 Qoder 安装。原生 Windows 创建 skills 软链接还
  需要开启 Developer Mode 或使用管理员终端。如果只在 skills 接线阶段报 `WinError 1314`，开启
  Developer Mode 后重新执行第 3 步；保留已经形成的有效安装目录，不要删除重来。
- 按上表的 MCP / 产品位置探测目标客户端。Windows 还包括
  `%APPDATA%/TRAE SOLO/User/`、`%APPDATA%/TRAE SOLO CN/User/`、
  `%LOCALAPPDATA%/Programs/TRAE SOLO/`、`%LOCALAPPDATA%/Programs/TRAE SOLO CN/`、
  `%LOCALAPPDATA%/Programs/WorkBuddy/` 和 `%LOCALAPPDATA%/Programs/Qoder/`；不得从一个产品的
  目录推断另一个产品。必须准确匹配两个 TRAE 目录名：只有 `TRAE SOLO CN` 不能证明已安装
  `TRAE SOLO`。macOS 必须分别探测 `/Applications/Qoder.app`、`/Applications/Qoder CN.app`、
  `/Applications/Trae.app`、`/Applications/Trae CN.app`、`/Applications/TRAE SOLO.app`、
  `/Applications/TRAE SOLO CN.app` 和 `/Applications/WorkBuddy.app`，不得互相推断；Intel 与
  Apple Silicon 使用相同路径和配置契约。
  优先配置当前正在执行本文的客户端。永久配置会在激活成功后接入所有选中的客户端，因此第 3 步前应向用户展示
  并确认探测列表。这是“全部同意或停止”的确认：只要用户不同意其中任一已探测 host，就在第 3 步前
  停止，不得静默选择。无法确定时询问用户，不要默默猜。

运行任何 Decision Engine 核心安装命令前，只询问两个 owner 值是否都已拿到，不得询问值本身：

> “你是否已经拿到 Decision Engine owner 提供的 endpoint 和设备激活密钥？只回答‘是’或‘否’，
> 不要把真实值发进聊天。”

如果宿主支持结构化选项弹窗，就显示“是”和“否”两个可选项，并把用户的选择作为明确答案；否则，在本中文流程中
只接受字面答案“是”或“否”，不得根据机器状态推断。

必须取得用户明确的“是”或“否”，并且只记住该答案。不得因为当前进程中缺少 `DE_ENDPOINT` 或
`DE_ACTIVATION_SECRET`、配置中没有相关值、设备尚未激活、用户未主动提及，或任何其他机器状态而
推断用户回答“否”。用户没有明确回答“是”或“否”时，owner 值状态仍为未知；必须重新询问，并且不得
开始 Decision Engine 核心安装。无论回答如何，都先安装或修复 AQG、完成 AQG standalone 配置，并让
AQG Doctor 通过后，才安装 Decision Engine 核心。如果回答“否”，继续核心安装，但只注册不含凭据的
DE Lite MCP 传输入口，最后必须使用第 8 步规定的简短文案。

当前私仓阶段，验证用户自己的普通 GitHub 读取权限：

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git ls-remote \
  https://github.com/deeppatternai/decision-engine.git \
  refs/heads/stable refs/heads/main
```

这两个 ref 都必须返回：`main` 提供安装驱动，`stable` 提供签名发布元数据。

允许 Git Credential Manager、macOS Keychain 或浏览器打开正常登录流程，但必须由用户本人完成。
不得索取 GitHub token、密码或 SSH 私钥，也不得把凭据写进 URL 或命令。

### 1. 确认固定安装目录为空

```bash
DE_INSTALL_ROOT="$HOME/.deeppattern/decision-engine"
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET test ! -e "$DE_INSTALL_ROOT" \
  && env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET test ! -L "$DE_INSTALL_ROOT"
```

只有命令成功才继续。如果路径已经是目录、文件、symlink、失效 symlink、junction/reparse point，或
类型无法确认，保留原路径并停止。报告“这不是第一次安装”；本文不执行迁移或恢复，也不要用
`DE_DEV_MODE=1` 绕过检查。

### 2. 准备干净的安装驱动 checkout（`DE_ROOT`）

`DE_ROOT` 只是运行 `install.sh` 的可信源码目录，不是最终安装目录。按顺序选择：

1. 环境中已设置且目录存在的 `$DE_ROOT`；
2. 用户明确指定的 Decision Engine checkout；
3. 一个新 clone。

没有可用 checkout 时，clone 到与固定安装目录不同的路径：

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET mkdir -p "$HOME/.deeppattern"
DE_ROOT="${DE_ROOT:-$HOME/.deeppattern/decision-engine-root}"
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git clone \
  --branch main \
  https://github.com/deeppatternai/decision-engine.git "$DE_ROOT"
```

`main` 是可信 `install.sh` 安装驱动的权威来源；第 3 步仍会独立解析并签名验证 `stable`
已发布的 release，再形成固定安装副本。

已有候选 checkout 时先只读检查。remote 只在内存中比较，只有精确匹配不含凭据的预期 URL 后才输出；
不得原样打印任意未知 remote URL：

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" status --porcelain
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" branch --show-current
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" rev-list --left-right --count 'HEAD...@{upstream}'
DE_ORIGIN_URL="$(env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" remote get-url origin)"
case "$DE_ORIGIN_URL" in
  https://github.com/deeppatternai/decision-engine.git|git@github.com:deeppatternai/decision-engine.git|ssh://git@github.com/deeppatternai/decision-engine.git)
    printf 'origin: %s (expected)\n' "$DE_ORIGIN_URL"
    ;;
  *)
    echo "origin: unexpected; preserve this checkout and use a fresh directory"
    ;;
esac
unset DE_ORIGIN_URL
```

只有它确实是预期的 Decision Engine checkout、位于 `main`、upstream 恰好是
`origin/main`、工作区干净且没有本地独有提交（`ahead=0`）时，才更新：

仍跟踪旧 `integration/managed-git-updater-p3-p5` 来源的 checkout 不要改指向，也不要继续复用；
将它保留为恢复副本，安装时改用下面显式锁定 `main` 的全新 clone。

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" pull --ff-only
```

如果存在未提交改动、untracked 文件、本地提交、upstream 缺失、分支或 remote 不符合预期、已经
分叉，或不能 fast-forward，保留原目录不动，换新目录继续：

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET mkdir -p "$HOME/.deeppattern"
DE_ROOT="$HOME/.deeppattern/decision-engine-root-fresh-$(env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET date +%Y%m%d%H%M%S)"
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git clone \
  --branch main \
  https://github.com/deeppatternai/decision-engine.git "$DE_ROOT"
```

执行任何代码前，向用户报告上面安全检查确认的预期 URL 和提交。不得显示不匹配的 remote URL：

```bash
env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET git -C "$DE_ROOT" rev-parse --short HEAD
```

### 3. 先验证 AQG，再安装 Decision Engine 核心

```bash
( cd "$DE_ROOT" && ./install.sh de )
```

本命令或第 4 步的显式 bootstrap 命令允许继承现有 `DE_ENDPOINT` 和 `DE_ACTIVATION_SECRET`。安装器会
先捕获它们，在启动无关子进程前从环境移除，并且只传给永久配置流程。

默认 `WITH_AQG=1`：先检查现有 AQG 是否健康。健康的 AQG 直接复用，不强制 pull 或重装；否则安装器
会安装或修复 AQG，自动安装它声明的 Python 依赖（包括 PyYAML），并要求 AQG Doctor 通过后，才会
宣布 AQG ready。只有用户明确要求不安装 AQG 时才使用 `WITH_AQG=0`：

```bash
( cd "$DE_ROOT" && WITH_AQG=0 ./install.sh de )
```

AQG ready 不是 AQG 安装的终点。默认 `WITH_AQG=1` 时，在 AQG 安装或修复且第一次 AQG Doctor 通过
后，Agent 必须继续执行 AQG 自己的完整 standalone 配置流程；完成之前，不得继续 Decision Engine
核心安装、永久激活、MCP 接线或 Decision Engine Doctor。

读取并遵守 AQG 的中文 standalone 文档：

```bash
"${PAGER:-less}" "$AQG_ROOT/AI_SETUP.zh-CN.md"
```

`AQG_ROOT` 必须指向 `https://github.com/deeppatternai/agent-quality-gates.git` 对应的 AQG
checkout。如果下面报告的 AQG remote 与该仓库不一致，保留现有 checkout，停止并要求用户提供正确的
AQG root。

使用 AQG 默认的 `installed-supported` 模式。先 dry-run：

```bash
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --aqg-root "$AQG_ROOT"
```

向用户展示 dry-run 摘要，包括 `AQG_ROOT`、remote、commit、detected clients、selected clients、
skipped clients、support-status、AQG 将写入的 skills、rules、hooks、MCP 配置，以及是否需要
`PROJECT_ROOT`。如果 dry-run 报出被跳过的 project-scope 客户端，例如 `qoder-cn requires --project-root`，
这表示在未提供 `PROJECT_ROOT` 时，AQG wrapper 会跳过这些 project-scope adapter，同时继续安装
其余已选客户端。这个 AQG project-scope profile 与上表中 DE 的 macOS `qoder-cn` Desktop MCP
adapter 是两套独立接入面；不得用其中一方的路径或探测结果证明另一方已安装。Agent 应报告被跳过的
客户端及原因，然后在用户明确确认后继续 `--apply`。只有当
用户明确要求安装这些被跳过的 project-scope 客户端，或后续 `--apply`、`--verify`、`--uninstall`、
`--is-installed` 命令实际仍需要 `PROJECT_ROOT` 才能运行时，才停止并要求用户提供一个已经存在的绝对
路径；不要只凭摘要里的 `PROJECT_ROOT` 就停。

用户明确确认 AQG standalone 改动后，再执行 apply：

```bash
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --aqg-root "$AQG_ROOT" --apply
```

如果 AQG standalone 流程要同步 Codex 或 Claude Code rules surface，必须严格按 AQG 文档执行：先
备份，只更新 AQG 受管 section，不得改动 AQG section 外内容。安装 AQG lifecycle hooks 前，必须告诉
用户这会启用 AQG hooks，可能包含 blocking gates，并等待用户确认。

AQG standalone 配置完成后，再运行 AQG Doctor：

```bash
python3 "$AQG_ROOT/scripts/aqg_doctor.py"
```

只要 AQG Doctor 出现任何 `FAIL`，必须先修复 AQG 并重跑 Doctor；不得宣称 Decision Engine 或 AQG
安装完成，也不得继续 Decision Engine 核心、激活、MCP 接线或 Decision Engine Doctor，直到完整
AQG standalone 配置完成且 AQG Doctor 通过。

AQG standalone 配置可以写入 AQG 自己的 skills、rules、hooks 和 MCP 条目。Decision Engine MCP
接线是另一条链路，只能由 Decision Engine 写入；不含凭据的传输入口可在激活前注册，激活状态决定
是否可以使用托管能力。Decision Engine 不再向 Codex 的全局或项目 `AGENTS.md` 写入路由说明。

这个 AQG gate 通过后，Decision Engine 安装流程才会在 `~/.deeppattern/decision-engine` 独立 clone
一份签名校验过的 `stable` 版本（跟 `DE_ROOT` 无关），安装核心、skills、更新器和不含凭据的 MCP
传输入口；只有两个 owner 值齐全时才尝试永久激活。

安装器会：

- clone 并签名验证当前 `stable` release；
- 将 Decision Engine 安装到 `~/.deeppattern/decision-engine`；
- 安装 Decision Engine skills；
- 复用健康 AQG，或先安装其依赖并验证通过；
- 要求 Agent 先完成 AQG standalone 配置并重跑 AQG Doctor，再继续任何 Decision Engine 核心、激活、
  MCP 接线或 Decision Engine Doctor 阶段；
- 把不含凭据的 `decision-engine` MCP 合并到上表所有选中的注册客户端，写入前自动备份并保留其他
  MCP server；
- 如果 Codex 全局 `AGENTS.md` 仍含旧版受控路由块，只在四个替代 skill 已就绪后安全移除；不新增路由块；
- 启用 Agent 后续启动时的自动更新检查；
- 使用 MCP 记录的同一个 Python 准备 tkinter 和 pywebview；
- 当前进程中两个 owner 值齐全时自动完成永久激活；
- 缺少 owner 值时正常暂缓激活，保留核心安装和 DE Lite MCP 传输入口，并保持托管能力关闭；
- 运行 Doctor。

默认 `WITH_AQG=1`，会安装、修复、验证 AQG 工程工具包，并继续进入 AQG 完整 standalone 配置流程。
只有用户明确要求只装 Decision Engine 时，才使用 `WITH_AQG=0 ./install.sh de`。

如果自动客户端探测不完整，继续第 4 步。Git 认证、签名、固定路径、AQG 安装、AQG standalone 配置、
AQG Doctor 或核心安装错误都停止本文流程。无论 MCP 传输入口是在安装时注册，还是激活后修复，接线
错误都必须如实报告。不能因为源码已经 clone 就误报安装成功。

### 4. 仅在自动探测失败时显式指定客户端

```bash
( cd "$DE_ROOT" && "$DE_PYTHON" -m installer.bootstrap_managed_install install \
    --client codex )
```

按需把 `codex` 换成上表任一注册 ID：`claude-code`、`claude-desktop`、`cursor`、`qoder`、
`qoder-cn`、`trae`、`trae-work`、`trae-cn`、`trae-work-cn` 或 `workbuddy`。需要多个客户端时，在同一命令中重复参数，例如
`--client codex --client qoder`。`DE_PYTHON` 必须已经指向第 0 步验证过的准确解释器。本命令只在
本次 bootstrap 中使用这些目标客户端。外层安装器可能已经注册不含凭据的 MCP 传输入口；本命令不
接收 endpoint 或 activation key。如果激活被推迟，第 5 步要复用相同参数。不要把 endpoint 或
activation key 放进命令参数。

### 5. 激活未完成时，由 Agent 打开永久配置窗口

如果安装输出已经确认永久激活成功，跳过本步骤。否则必须取得明确答案，并询问用户：

> “你是否已经拿到 Decision Engine owner 提供的 endpoint 和设备激活密钥？只回答‘是’或‘否’，
> 不要把真实值发进聊天。”

不得根据环境变量、配置、激活状态或任何其他机器状态推断答案。如果用户没有明确回答“是”或“否”，
必须重新询问，并且不得继续本步骤。
宿主提供结构化选项时使用“是”/“否”选项；否则按上面的规则只接受字面答案。

打开窗口前先读安装输出和 Doctor。如果出现 `owner-guided recovery required`、
`persistence is uncertain` 或 `do not retry automatically`，不要再次激活。保留
`.runtime/activation-recovery-required.json`，并报告需要 Decision Engine owner 或支持人员处理。
只有普通的“缺少值而跳过”或“值被拒绝”才继续下面流程。

如果两个值已经在 Agent 当前进程环境中，通过下列命令重试，secret 不进入 argv：

```bash
( cd "$HOME/.deeppattern/decision-engine" && "$DE_PYTHON" -m installer.permanent_setup --from-env )
```

否则，用户回答“是”后，Agent 先说明会弹出掩码桌面窗口。当前宿主是 WorkBuddy 时，无论其 sandbox
当前是否启用，都使用 WorkBuddy 的 Bash 工具执行下面的 WorkBuddy 专用命令，并设置
`dangerouslyDisableSandbox: true`。说明这需要用户批准，等待批准后再让命令在 sandbox 外运行。命令级
标记用于标识当前宿主，即使中间 launcher 脱离了 WorkBuddy 的祖先进程链也能可靠识别。如果用户拒绝，
或 WorkBuddy 无法提供经批准的 sandbox 外执行，则展示该命令，让用户在普通桌面终端中运行。

在 Windows 上，WorkBuddy 的启动链无法可靠渲染 WebView 表单（即使批准在 sandbox 外执行，也可能
只画出一个无法输入的空框）。因此该标记始终直接打开现有的、功能完整的掩码 Tk 表单。这是受支持的
WorkBuddy 激活界面，无需重跑。在其他平台上，该标记不会改变后端选择；现有桌面会话保护仍要求命令
在 sandbox 外执行。

在 macOS 上，必须在 WorkBuddy 的 Bash 工具（仍需按上文说明，在用户批准后设置
`dangerouslyDisableSandbox: true` 并在 sandbox 外运行）或普通桌面终端中直接执行下方 shell 命令。
不得生成或通过 Finder 打开 `.command` 包装文件。如果该包装文件由处于 sandbox 内的 WorkBuddy 操作
发起，Sandbox 可能会在 Python 启动前阻止它转交给 Terminal；由此出现的权限对话框不是激活界面。
命令成功启动后会打开掩码 pywebview 表单；只有 pywebview 在原生注册前不可用时才回退 Tk。

WorkBuddy（macOS、Linux、WSL 或原生 Windows Git Bash）：

```bash
( cd "$HOME/.deeppattern/decision-engine" && \
  env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET \
  DE_WORKBUDDY_SETUP=1 \
  "$DE_PYTHON" -m installer.permanent_setup )
```

其他宿主仍按原流程主动执行当前平台命令。不要让用户自行 `export`，也不要让用户在聊天中输入。

macOS / Linux / WSL，或原生 Windows Git Bash：

```bash
( cd "$HOME/.deeppattern/decision-engine" && \
  env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET "$DE_PYTHON" -m installer.permanent_setup )
```

除 WorkBuddy 外的宿主，只有原生 Git Bash 安装可以改用原生 Windows PowerShell；WSL 安装不能使用下面命令。Agent 必须把
下面的 `$PythonPath` 替换成第 0 步打印的准确绝对解释器路径，不得猜测或静默选择另一个 Python：

```powershell
$Root = "$env:USERPROFILE\.deeppattern\decision-engine"
$PythonPath = "<第 0 步打印的 Python 绝对路径>"
$env:DE_ENDPOINT = $null
$env:DE_ACTIVATION_SECRET = $null
if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "Decision Engine installation directory is missing"
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "The Python interpreter used for installation is missing"
}
Set-Location -LiteralPath $Root -ErrorAction Stop
& $PythonPath -m installer.permanent_setup
if ($LASTEXITCODE -ne 0) {
    throw "Decision Engine permanent setup failed"
}
```

将 `DE_PYTHON` 设为第 0 步验证过的准确解释器路径。用户只在弹窗中输入 endpoint 和掩码 secret。
程序直接在内存中激活；只有激活成功后，才原子保存
endpoint 和服务端签发的 device/access/refresh 凭据；不保存原始 activation secret；随后以先备份
方式修复 MCP，并运行 Doctor。成功后关闭终端、重启 Agent 或重启电脑都不需要重复输入。

如果第 4 步使用了显式客户端选择，请把相同且可重复的 `--client` 参数附加到永久配置命令，例如
`--client codex --client qoder`。这样会把用户确认过的选择带过激活边界；全部注册宿主都遵守相同的
激活后接线规则。未传参数时，永久配置会接入检测到的全部受支持客户端。

已经激活的设备不会再次索取 secret，也不会重复占用设备名额。用户取消窗口时不写凭据，必须报告
“用户取消”，不能报告成功。如果激活成功后 MCP 或 Doctor 失败，保留服务端签发的设备凭据，修复
输出指明的阶段后再运行永久配置；不得再次索取 owner 值或重复占用设备名额。

如果无法打开 GUI，停止并报告“掩码配置窗口不可用”。不得退回聊天、`.bashrc`、`setx` 或普通文件
收集或保存 secret。

这两个值不能从 GitHub、Codex、Claude 或 OpenAI 账号中生成。用户应向邀请者、组织内 Decision
Engine 管理员或服务 owner 获取。以后拿到两个值时，只需在 Agent 再次询问上述问题时回答“是”，
由 Agent 回到本步骤打开窗口；无需重新安装。

### 6. 验证实际安装

核心安装后先在本地运行 Doctor。永久激活成功前，已注册的 MCP 条目可能只开放 DE Lite 路径，不得
把它当作托管服务已经可用。激活后，GUI app 只有在进程启动时才继承 PATH；此时再让用户**彻底退出
并重开**上表中每一个已配置宿主，然后让 Agent 确认：

- 让重启后的 host 调用 `list_auditors`，确认返回审核员列表。
- 如果配置了 Claude Desktop，其上表对应的平台路径必须包含该 server。
- 下列每个 JSON/TOML 检查都只能解析 `decision-engine` server 键是否存在；不得打印完整宿主配置、
  其中的 environment 块或任何值。
- 如果配置了 Cursor，`~/.cursor/mcp.json` 必须包含 `decision-engine` server 键。
- 如果配置了 TRAE Work，上表对应的 Windows 或 macOS 路径必须包含该 server。
- 如果配置了 TRAE Work CN，上表对应的 Windows 或 macOS 路径必须包含该 server。
- 如果配置了 WorkBuddy，`~/.workbuddy/mcp.json` 必须包含该 server。
- 如果配置了 Qoder，请使用表格中的对应平台路径；Qoder CN 必须使用
  `~/.qoder-cn/settings.json`。不要检查任一 Qoder IDE profile。

```bash
( cd "$HOME/.deeppattern/decision-engine" && \
  env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET "$DE_PYTHON" -m installer.doctor )
```

Doctor 必须从实际安装目录运行，而不是 `$DE_ROOT`。逐行检查：

- `python`、`skills` 应通过；激活后 `mcp` 和 `python-wiring` 也应通过；
- `dev-mode` 应显示未处于开发模式；
- `setup-gui` 报告 tkinter 状态；
- `popup` 报告 pywebview 和系统 GUI backend；GUI 警告不否定核心 MCP 安装，但必须如实报告；
- 未提供 owner 值时，`activation: not activated yet` 是预期状态；MCP 条目可以启动本地 DE Lite shim，
  但托管能力必须保持不可用；
- 完成第 5 步后，即使所有 `DE_*` 环境变量都不存在，`activation` 仍应显示设备已激活；
- `libreoffice` 是可选项；
- 第一次完整重启前，`update` 可能仍显示 `running=unconfirmed`；第 7 步重启后必须确认实际运行提交。

如果任何真实检查显示 **FAIL** 或 `✗`，先报告，再只按它明确给出的修复建议处理。不得删除、reset 或
覆盖安装目录；真实错误未解决前不得宣称成功。

### 7. 激活成功后完整重启并冒烟测试 MCP

激活暂缓时跳过本步骤。永久激活成功后，完整重启宿主，让 MCP 进程重新加载已保存凭据和托管能力。
告诉用户彻底退出并重新打开上表中每一个已配置宿主；只新建任务或标签页不够。
重启后让 Agent 执行：

> 调用 `list_auditors` 工具并显示结果。

- 已激活时，返回 auditor 列表表示 MCP、设备激活和服务端链路正常；
- 工具不存在，说明 MCP 配置没有被加载。检查实际配置的客户端，并确认此前是彻底退出后重启。

重启后再运行一次 Doctor，确认 `update` 显示真实 `running=<commit>`，而不是 `unconfirmed`。

### 8. 向用户清楚报告

用户完整重启之前，应说“**安装步骤已完成，等待重启确认运行**”，不要说所有功能已经验证完成。
报告：

- 实际安装版本和提交；
- tkinter 和 pywebview 状态；
- 配置了哪些客户端；
- MCP 接线是新增、更新还是已存在，以及备份路径；
- 设备已永久激活，还是有意暂缓；
- 是否仍需完整重启 host；
- 如果使用了 fresh driver checkout，原 checkout 保留在哪里。

已经永久激活时，明确说明设备凭据已经保存，不再依赖 Git Bash 或任何 `DE_*` 环境变量。重启后
`list_auditors` 必须返回 auditor 列表，才表示整条链路正常。

未提供 owner 值时，应把本地 DE Lite 传输入口与激活状态分开报告，不得宣称托管能力已经配置；也不要
只为托管能力要求用户重启 host。必须用下面的简短文案结尾：

```text
AQG standalone 配置已完成，AQG Doctor 已通过。Decision Engine 尚未激活。请向 Decision Engine
owner 获取 endpoint 和设备激活密钥；获取后不要把真实值发送到聊天，只需告诉我“继续安装 DE”。
```

不要把“暂未激活”说成“安装失败”，也不要把真实失败说成成功。

---

## Agent 必须遵守的边界

- 只执行本文列出的安装、激活、MCP 合并和验证步骤；不碰生产、密钥、分支保护或无关项目。
- 不得手工打印、记录、提交或代用户输入 GitHub token、密码、SSH 私钥、API key、设备 token、owner
  值或完整配置文件。唯一的持久化例外是 `installer.permanent_setup`：激活成功后可保存 endpoint 和
  服务端签发的设备凭据，但绝不保存 activation secret。
- 使用安装器提供的先备份 MCP 合并和修复命令；不得手工覆盖 Agent 配置，必须保留其他 MCP server。
- 安装驱动 checkout 异常时换新目录；固定安装路径存在或异常时停止。不要混淆两个目录。
- 不删除、reset、强制 checkout、stash、clean、移动或覆盖用户已有文件。
- 登录和掩码凭据输入必须由用户本人完成。Agent 负责打开获准的输入窗口、继续验证，并如实报告不含
  敏感信息的错误。

---

## 分发说明

本文随完整 Decision Engine 客户端一起分发，不应脱离 `install.sh`、`installer/` 和发布签名信任材料
单独使用。安装驱动从仓库权威 `main` 分支获取；实际运行代码仍来自 `stable` 签名元数据和不可变
release tag。本文不硬编码任何 server endpoint 或 secret。
