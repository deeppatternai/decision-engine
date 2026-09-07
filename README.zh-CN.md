# Decision Engine

[English](README.md) · **简体中文** · **v0.2.80**

> **一个你真正敢信的第二意见 —— 因为它来自许多个彼此独立的头脑，而不是一个模型在自我认同。**

**Decision Engine（DE）** 是一个面向「一旦做错代价高昂」的决策的托管引擎。把你的代码、
方案、想法、市场问题或预测，摆到一个**由前沿模型组成的跨厂商评审组（cross-vendor
panel of frontier models）**面前 —— 一组来自不同训练分布、彼此真正独立的评审者 —— 拿回
一份结构化、已裁决的解读：哪里有问题、哪里过硬、评审者在哪里一致、又在哪里分歧。你用
自然语言，从你本就在用的编码 agent 里调用它；所有的智能都运行在服务端。

前提很朴素：**一个模型看不见自己的盲点。** 让单个模型检查自己的产出，它只会倚靠它被
训练时的那套假设。把同一个问题交给一组**不共享训练分布**的评审者 —— 他们**各自独立地
收敛**到一起的地方，就是真信号；而他们分歧的地方，恰恰是你该多看两眼的地方。Decision
Engine 的一切，都是围绕这个差别造出来的。

而 Decision Engine 能做的，不止是*评审*一个决策。它还能替你把决策**画出来** —— 把一团
缠在一起的想法，变成你一眼就看懂的图 —— 还能递给你一块**白板**，让你和 AI 一起用手把
方案理出来。

## 为什么是 Decision Engine —— 设计哲学

**要的是独立的头脑，而不是把一个头脑调得更响。** 价值不在于更多的模型调用，而在于
*互不相关*的调用。一个横跨不同厂商与训练分布的评审组，能抓到任何单一血统会系统性漏掉的
东西。来自真正独立评审者之间的一致，赢得信任的方式，是一个自信的单一答案永远做不到的 ——
护城河是方法，而不是任何一个模型的名字。

**你是裁决者，不是观众。** DE 呈上意见，但从不凌驾于你的判断之上。结论以结构化输入的
形式回到*你*（或你的 agent）手里，供你 accept、reject 或升级 —— 而不是一纸下达的判决。
评审组给建议，你来定夺。一次评审是一个决策的开始，不是它的终点。

**对不确定性诚实。** DE 会告诉你评审组是*真的*达成一致，还是只是锚定在同一个措辞上，
并把这种收敛质量明确标注出来，而不是伪装出信心。预测只汇集真实预测者与市场已有的判断 ——
引擎绝不自产任何一个数字。一个你无法校准的信任信号，比没有信号更糟。

**要的是纪律，而不是救火。** 最有价值的评审发生在 chokepoint —— 在你 commit 之前、
在你发布之前 —— 那个第二意见还来得及改变结局的时刻，而不是上线之后的事后复盘。DE 就是
为坐镇那一刻而造的。

Decision Engine 可独立运行，同时它也是**补全
[Agent Quality Gates（AQG）](https://github.com/deeppatternai/agent-quality-gates)** 的托管引擎 ——
AQG 是本地、无需账户的工程纪律工具包。安装其中任一个，都可以在同一步把另一个一并带上。

> **状态：** 内测。Decision Engine 需要 owner 发放的 endpoint + 设备激活密钥才能激活设备。
> AQG 是本地的，无需账户。

## 不只是被评审 —— 还能被*看见*、被*塑形*

大多数 AI 只用一种形态回答你：更多文字。可一个决策最难的部分，往往不是拿到一个*意见* ——
而是*看懂*行业黑话底下的那个东西，以及*说清楚*你到底想要什么。Decision Engine 各给了你
一件趁手的工具 —— 而且都不是一个聊天框。

### 🎨 把想法看见，而不是去猜 —— `/graphic-explanation`

让引擎解释一个概念、一段逻辑，或者你正在进行的这场对话 —— 它不会再甩来一堵文字墙，而是
递给你一张**图**：一格格**漫画**把故事讲出来，一张**信息图**把结构铺开，或者一张干净的
**图解**，把那个确切的流程、时序、状态机或架构画清楚。看懂，不再是一道解黑话的苦差，
而是你**一眼就看见**的事。

图会在自己的窗口里打开，**旁边就是一个追问对话框** —— 指着任何一处问一句「等等，这里
为什么？」，把线头一直抽到它「咔哒」一声想通。再也不用在别人的行业黑话里连蒙带猜。

### ✍️ 在白板上把事情想明白 —— *和* AI 一起 —— `/discussion-board`

有些事写成一段话怎么都说不清，可一旦能动手挪那些块，立刻就明白了。开一块讨论板，AI
**已经先替你把话题铺排好** —— 排成可以重新排序、调优先级的卡片，或者一张图片、一份文档
让你标注。然后轮到你上手：**拖动、排序、涂写、圈画、划掉、在边上留一句批注。**

把标注好的白板交回去，AI 从你**做了什么**里读懂你的意图 —— 而不是从你勉强打出来的字里
猜。这是一场和 AI 在同一块画布上真正的工作会 —— 你脑子里那个模糊的想法，回来时已是一份
**具体的、共享的方案。**（现在是看板；结构化的图表板 —— 流程图、思维导图、旅程图等 ——
正在陆续加入。）

## 带着一个半成形的想法来 —— 带走一个你守得住的

不是每个决策，一开始都是一个可以拿来评审的成品。有时你手里只有一个直觉。两个 skill
从这里接手 —— 而且都是*和*你一起想，不是*对着*你想。

### 🌱 把模糊的想法，磨成能验证的 —— `/audit-explore`

你有一个粗糙的直觉 —— *「也许我们该……」* —— 但还说不清什么能证明它对、什么能证明它错。
`/audit-explore` 把它发展成一个**成形的、可证伪的假设**：一个跨厂商评审组，从真正不同的
角度去探这片问题空间；一次预演式的 premortem，在你还没花一分钱*之前*先问它会怎么失败；
你最后拿到的，是一个足够锋利、可以拿去验证的主张 —— 也随时可以交给下面任何一个 skill。

### 🎯 让一屋子独立的头脑替它做压力测试 —— `/audit-brainstorming`

一旦想法有了形状，`/audit-brainstorming` 把它摆到一个「思考伙伴」评审组面前 —— 不是去
挑 bug，而是陪你把它想透：哪里过硬、哪里脆弱、你没说出口的反方论点、你没察觉自己已经
默认下来的假设。它也把诚实的认识论一并交回来 —— 什么能*证伪*它、基准率是多少、证据到底
该把你的信心往上调还是往下调。

两个合起来，把*「我觉得我们应该……」*，变成*「这是这一注、这是为什么、以及到底什么会
证明我错了。」*

## 把它对准外面的世界 —— 市场，和赔率

有些问题跟你的产物根本无关 —— 它们关乎*外面*正在发生什么。两个 skill 把引擎转向外部。

### 📊 一份不是单个模型瞎猜的市场解读 —— `/audit-market-research`

问一个市场、GTM 或定位问题，`/audit-market-research` 给你搭一份**洞察文档** —— 但不是从
单个模型的想象里来。一个跨厂商评审组从彼此独立的角度分析它，**扎根在真实检索上**（横跨
网页、社媒、财经多个来源），并（可选）过一遍**合成客户评审组**。最妙的是，它会告诉你
*该信几分*：一个诚实的**收敛质量**信号，把「评审者真的一致」和「他们只是抓住了同一个
措辞」分开。

### 🎲 把赔率汇集起来 —— 而不是自己编 —— `/audit-forecast`

对一个具体的、可验证的、有时限的结局 —— 一场比赛的胜负、一次选举、一份财报的超预期、
一个价格阈值 —— `/audit-forecast` 读的是**世界已经怎么预测它**：博彩赔率、真金白银的
预测市场、数据与模型的盘口、专家与媒体的判断 —— 汇集成一份结构化的解读（方向、概率、
共识在哪里强、在哪里分裂、关键催化剂、基准率、什么会把它翻盘）。它**绝不自造任何一个
数字** —— 每个数都能追溯回一个真实来源，服务端强制。一份诚实的汇总，胜过一个自信的幻觉。

## 本仓分发什么（以及不分发什么）

| 随本仓分发 | 仅存在于托管服务端（永不随仓分发） |
|---|---|
| 安装器（`installer/`） | 提示词、模型/声部阵容、编排 |
| 纯传输的 MCP shim | board / diagram 渲染 —— HTML、布局、生成 |
| 原生弹窗显示外壳（逐字显示服务端渲染的 board 与 diagram；自身不含渲染逻辑） | 调研 / 预测流水线 |
| 你的本地设备配置（安装时写入） | 广告逻辑、服务器地址、密钥 |
| 这些文档 | — |

客户端只做**显示与传输**：shim 把每一条 JSON-RPC 消息原样转发到服务端的 `/mcp` 端点、
只搬运字节，原生弹窗外壳则逐字显示服务端回传的内容 —— 交互式 board 和每一张 diagram 都在
服务端生成、以成品字节递过来，所以外壳自身不含任何工具 schema、提示词、阵容、布局或渲染
逻辑。哪些工具存在，以服务端为唯一真相源。红线（red-line）的具体内容与验证方式见
[`installer/LEAK_SCAN.md`](installer/LEAK_SCAN.md)。

## 你能用它做什么

安装 DE 会把一组 skill 路由进你的 agent。可以按名字调用（例如 `/audit`），也可以直接
描述任务 —— agent 会挑选 skill 并把请求转发给托管引擎。一句话速览：

| skill | 做什么 |
|---|---|
| `/audit` | 对一份产物（代码、文档、方案、迁移、设计）做外部审计。跨厂商评审组挑出缺陷；由你的 agent 裁决成一个结论。 |
| `/audit-adjudication` | 把一份或多份既有审计结果，归并成一张统一的 accept / reject 裁决表。 |
| `/audit-brainstorming` | 用一个思考伙伴评审组对一个想法、策略或提案做压力测试 —— 给出优势、风险、反论点、假设 —— 而非挑缺陷。 |
| `/audit-explore` | 把一个模糊、未成形的想法发展成一个成形、可证伪的假设，带 premortem 脚手架，可交棒给下面的 skill。 |
| `/audit-forecast` | 汇总外部预测者与市场**当前**对某个具体、可验证、有时限的结果的预测。它只汇集既有预测 —— 绝不自产预测。 |
| `/audit-market-research` | 生成一份市场 / GTM 洞察文档：跨厂商分析 + 搜索取证的事实检索 + 一个诚实的收敛质量信号。 |
| `/audit-writing-plans` | 把上游审计结论转成结构化的工程 / 实施文档，在晋级前经评审组交叉验证。 |
| `/graphic-explanation` | 可视化讲解当前对话或某个决策 —— 引擎返回一张成品漫画、信息图或 SVG 示意图，在原生弹窗中打开。 |
| `/discussion-board` | 把一个决策或一组条目打开成交互式看板弹窗（可拖拽卡片、优先级、行内编辑、备注、手绘标注），你手动调整后再读回结果。 |
| `/layer-check` | 本地推理纪律，用于捕捉对比 / 竞争分析里的范畴错误（把「不同层级的产品当成替代品」的坑）。完全在你本机运行。 |

一次安装即可服务下列全部注册宿主，不需要挑选按 agent 区分的 Decision Engine 变体。
除 `/layer-check` 外，所有 skill 都会触达托管引擎、需要一台已激活的设备（见下文）；
`/layer-check` 是本地的，无需账户即可用。

| 宿主 | 安装器 ID | 当前本地宿主范围 |
|---|---|---|
| Claude Code | `claude-code` | MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |
| Claude Desktop | `claude-desktop` | MCP、原生窗口、弹窗右侧追问、Stop Panel |
| 腾讯 CodeBuddy Agent CLI | `codebuddy` | 仅支持独立 Agent CLI；CodeBuddy Studio 是另一个未支持产品；MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |
| Codex | `codex` | MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |
| Cursor | `cursor` | MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |
| 阿里 Qoder Desktop | `qoder` | Windows Desktop 1.106.3+；macOS Qoder.app 0.1.3+；MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |
| 阿里 Qoder CN Desktop | `qoder-cn` | macOS Qoder CN.app 0.1.4；MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |
| 阿里 Qoder IDE | `qoder-ide` | macOS Qoder IDE.app 1.106.3+；独立 MCP 身份、共享 Qoder Skills 与审计 hook、原生窗口、弹窗右侧追问、Stop Panel |
| 阿里 Qoder CN IDE | `qoder-cn-ide` | macOS Qoder CN IDE.app 1.106.3+；独立 MCP 身份、共享 Qoder CN Skills 与审计 hook、原生窗口、弹窗右侧追问、Stop Panel |
| TRAE Desktop | `trae` | macOS Trae.app 3.5.81；MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |
| TRAE Work | `trae-work` | Windows and macOS Desktop 0.1.48+；MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |
| TRAE CN Desktop | `trae-cn` | macOS Trae CN.app 3.3.95；MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |
| TRAE Work CN | `trae-work-cn` | Windows and macOS Desktop 0.1.48+；MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |
| 腾讯 WorkBuddy Desktop | `workbuddy` | Windows and macOS Desktop；MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |
| 腾讯 WorkBuddy AI Desktop | `workbuddy-ai` | macOS WorkBuddy AI.app 5.5.2+；MCP、Skills、原生窗口、弹窗右侧追问、Stop Panel |

已注册的 Qoder、TRAE 与 WorkBuddy Desktop 产品系列以及 CodeBuddy Agent CLI 现在都提供服务端追问对话（由 hub 托管、
不启动本地 agent）；旧的本地 CLI 追问路径仍只限于具备该契约的宿主。它们的 Graphic
Explanation / Discussion Board 原生窗口和审计 Stop Panel 仍复用公共实现。
它们在 macOS Intel 与 Apple Silicon 上使用同一套宿主契约，无需按芯片配置 Decision Engine。

### 深度 & 即将到来

**`/audit` 有三档深度 —— 每一档是不同的评审组，不是同一组调深浅。**

- **fast** —— 固定的跨厂商快扫，低推理强度。给 trivial 改动做 sanity check。
- **standard**（默认）—— 横跨若干个不同训练分布的评审组，满推理强度。日常代码、文档、
  方案。
- **deep** —— 在 standard 评审组之上，再加来自更多训练分布的独立声音，并以更高的推理
  effort 运行。高 stakes / 安全敏感 / 不可逆的活。

**`/graphic-explanation`** 出**漫画**、**信息图**，以及服务端 authoring 的 **SVG 示意图**
（流程图、时序图、状态机、架构图）—— 每一种都在服务端生成、以成品返回。

**`/discussion-board`** 目前给你 **kanban** 卡片板，外加对丢进来的一张**图片**或一份多页
**文档**做手绘标注。*即将到来：*

- **结构化图板 —— 全部 24 种**：流程图、思维导图、组织架构图、四象限、循环图、泳道图、
  时间轴、时序图、状态机、甘特图、维恩图、鱼骨图、漏斗/金字塔、树图、概念图、决策矩阵、
  实体关系图（ER）、真值表、亲和图（KJ）、决策表、SWOT、商业模式画布、用户旅程图、
  用户故事地图。每一种都由引擎在**服务端**渲染；你查看、标注、提交回来。
- **Office 与 PDF 文件** —— 把 DOCX / PPTX / XLSX / PDF 丢上板做标注。PDF 开箱即用；
  Office 文件在你本机经 LibreOffice 转换 —— 按需安装（见[环境要求](#环境要求)）。

> 标注为*即将到来*的项目，内测阶段尚未开放。

## 安装

### 环境要求

客户端本身是 stdlib Python —— 安装它不需要虚拟环境。只有**交互式画板**需要两个
额外依赖，且客户端都替你搞定：

| 依赖 | 用于 | 如何安装 |
|---|---|---|
| **Python 3.12+** | 一切 | 安装指南会复用满足最低版本且通过检查的本地版本；安装器会在安装前检查版本、SSL、venv、pip 和 Tk 支持 |
| **pywebview** | 画板 / 图解 / 图形解释的原生弹窗 | **首次使用时自动安装** —— 首次打开画板时,launcher 会把它 pip 装进客户端自己的环境。无需你操作。 |
| **LibreOffice** | *可选* —— 把 **Office** 文件（DOCX / PPTX / XLSX）转上板。PDF 板无需它 | **按需** —— 需要时跑 `python3 -m installer.office`：macOS 经 Homebrew 装；Linux 打印那一行 `apt`/`dnf` 让你跑。`de doctor` 会在缺失时提示。也可以自己提前装。 |

随时跑 `python3 -m installer.doctor` 检查你的环境（skills 是否 link、Python、弹窗
后端、LibreOffice）—— 见[检查你的环境](#检查你的环境)。

### 安装 Decision Engine + AQG，再完成激活（推荐）

Decision Engine 客户端 bundle 就随本仓分发。先克隆本仓，再从你的本地检出安装 ——
不要把 owner 发放的 activation secret 放进命令或环境变量赋值；核心安装完成后，通过掩码的永久
配置窗口输入：

```bash
git clone https://github.com/deeppatternai/decision-engine.git
cd decision-engine
./install.sh
( cd "$HOME/.deeppattern/decision-engine" && python3 -m installer.permanent_setup )
```

这份 clone 只是用来*跑* `install.sh` 的 —— `install.sh` 自己会独立地在固定路径
`~/.deeppattern/decision-engine` 重新 clone 并校验一份真正会被自动更新管理的副本，跟你这次 clone
到哪没关系。掩码永久配置会激活这份安装副本并写入宿主 MCP。AQG（从它自己的公开仓克隆）会并排落在
`~/.deeppattern/agent-quality-gates`，两者的 skill 都路由进你的 agent skill 目录
（`~/.claude/skills/` 和 `~/.codex/skills/`）。探测到桌面宿主时，同一组受管 Skills 还会按
注册宿主路由到 `~/.cursor/skills/`、`~/.trae/skills/`、`~/.trae-cn/skills/`、
`~/.workbuddy/skills/`、`~/.workbuddy-ai/skills/`、`~/.codebuddy/skills/`、
`~/.qoder/skills/` 或 `~/.qoder-cn/skills/`。`install de`（默认）会把两个都装上。
若想从 AQG 一侧起步、并在同一步加上 DE，用 `WITH_DE=1 ./install.sh aqg`（见下）。永久配置只保存
endpoint 和服务端签发的每设备凭据，绝不保存 owner 发放的 activation secret。

> **在开发本仓库，或者你在固定路径上已经有一份不是刚 clone 出来的 checkout？**
> 改用 `DE_DEV_MODE=1 ./install.sh`——这会让 agent 直接指向你正在跑的这份 checkout
> （不做签名校验、不自动更新），改代码立刻生效。

### 只装 Decision Engine（无需写代码）

Decision Engine 本身就能独立使用。如果你用 AI 来做决策、汇总选项、给方案把关或修改
文档 —— 而且你并不写代码 —— 那你要的就是 DE，不需要那套工程纪律工具包。设 `WITH_AQG=0`：

```bash
git clone https://github.com/deeppatternai/decision-engine.git
cd decision-engine
WITH_AQG=0 ./install.sh
( cd "$HOME/.deeppattern/decision-engine" && python3 -m installer.permanent_setup )
```

（跟上面一样，这份 clone 只是用来跑 `install.sh`；真正的副本会独立落在
`~/.deeppattern/decision-engine`。）这只装 Decision Engine、完全跳过 AQG —— 不从 AQG 仓克隆任何东西，也不加任何工程 gate。
你依然拿到全套引擎 skill（评审、市场调研、预测、可视化讲解、看板）。之后改主意了？
去掉 `WITH_AQG=0` 重跑一次（或 `./install.sh aqg`）即可加上 AQG。

### 只装 AQG（本地，无需账户 —— 不推荐单独安装）

```bash
./install.sh aqg
```

克隆 [`deeppatternai/agent-quality-gates`](https://github.com/deeppatternai/agent-quality-gates)
并运行它的安装器。无需 endpoint 或账户。

> ⚠️ **不推荐单独安装。** AQG 的若干纪律 gate —— commit 前的外部评审、多维评审、
> 以及 phase-transition 审计检查点 —— 在其关键一步会交棒给 Decision Engine 的审计引擎。
> 没有 DE 时，这些 gate 只能发出一个建议，跑不了真正的跨厂商审核 —— 于是你拿到了脚手架，
> 却拿不到第二意见。请把 DE 和 AQG 一起装（上面的 `de` 路径，或下面的 `WITH_DE=1`）以获得
> 完整体验。

若想从 AQG 一侧同时装上 Decision Engine，设置 `WITH_DE=1`，然后仍通过同一个掩码窗口激活：

```bash
WITH_DE=1 ./install.sh aqg
( cd "$HOME/.deeppattern/decision-engine" && python3 -m installer.permanent_setup )
```

> ⚠️ **AQG 仓必须从你的机器可达** —— 要么它是公开的，要么你的 `git` 已对它鉴权。
> `de` 和 `aqg` 两条路径都从 AQG 自己的公开仓克隆它，所以那次克隆若失败，AQG 就装不上。
> 若你从别处安装 AQG，可用 `AQG_REPO=<git-url> ./install.sh aqg` 覆盖来源。

### 从 bundle 手动 / 离线安装

上层的 `./install.sh` 会替你调用这一步。若要直接用
[`installer/`](installer/) 里的 Python 安装器针对一个 bundle 根目录（例如本仓，或
检出在别处的一个 bundle）驱动：

```bash
python3 -m installer.install de \
  --bundle-root ./bundle
```

这个底层原语只铺设 body，不会建立 `installer.permanent_setup` 所要求的签名受管安装身份。交互式激活
应使用上面的普通受管安装路径。历史/预配置自动化和显式 MCP 修复细节见
[`installer/README.md`](installer/README.md)；不得把 activation secret 放进 argv。

## 激活并使用

Decision Engine 按设备绑定。把 shim 向你的 agent 注册一次：

```bash
python3 -m installer.mcp_config    # 打印可直接粘贴的 MCP server 条目
```

安装后让 Agent 主动运行永久配置：

```bash
( cd "$HOME/.deeppattern/decision-engine" && python3 -m installer.permanent_setup )
```

macOS 上的永久配置会打开掩码 pywebview 桌面窗口；只有 pywebview 在原生注册前不可用时才回退 Tk。
Windows 默认采用相同路径，但 WorkBuddy 会直接打开受支持的掩码 Tk 表单。用户只输入一次 owner
endpoint 和 secret；每用户配置只保存 endpoint 和服务端签发的设备凭据，以后关闭终端、重启 Agent
或重启电脑都无需重复输入。设备激活密钥不进入聊天、命令参数、环境变量或持久配置。

已经激活的设备再次运行永久配置时只修复 MCP 并运行 Doctor，不会自动轮换或重新绑定设备，也不会
占用新的设备名额。设备被撤销或 owner endpoint 变更时，需要使用单独、明确的 owner 引导恢复流程。
现有的首次使用激活弹窗仍为旧版/已预配 endpoint 的安装保留；managed 新用户应优先使用 Agent 主动
运行的永久配置流程。

完整细节（包括 fail-closed 的明文凭据规则）见
[`installer/README.md`](installer/README.md)。

## 检查你的环境

`de doctor` —— 一个一次性诊断（和 AQG 一样），一处告诉你客户端是否就绪、不就绪该修什么：

```bash
python3 -m installer.doctor          # 人读报告
python3 -m installer.doctor --json   # 机器可读
```

它检查：Python 是否 3.12+、skills 是否已 link 进你的 agent、原生弹窗后端（pywebview）
能否开窗、LibreOffice 是否可用于 Office 转换（没有则 WARN —— 它可选、按需安装）、以及
本设备是否已激活。全部 PASS 或 WARN 时退出 `0`，真正坏了才退出 `1`。它**绝不**打印你的
endpoint 或激活码 —— 只报是否已设置。umbrella `./install.sh` 会在安装结尾替你跑一遍。

## 安装副本与更新

MCP 条目运行 `installer.launcher`。在发布迁移的过渡期，它仍指向当前确实可运行的源码/旧副本；
只有签名受管激活已经发布 launcher 协议后，才切到固定的
`~/.deeppattern/decision-engine` checkout。旧安装所使用的源码 clone 目前不能删除；迁移完成后，
受管产品副本才与任何开发工作区彼此独立。

在发布迁移的过渡期，旧的复制式安装没有受管控制面；launcher 会继续运行旧副本，但不会联网
或修改 Git。此类安装仍通过任意源码 clone 拉取后重跑安装器更新：

```bash
git pull
./install.sh
```

重跑是幂等的，并保留已有设备激活。正式 `stable`、生产公钥、Gitee 镜像和一次性签名迁移
bootstrap 发布后，已迁移安装会在 MCP 启动前按有界的 GitHub→Gitee 策略更新。
`python3 -m installer.doctor` 会分别显示 installed、target、running 和 last result，避免把
“已下载”误报成“当前已经运行”。

## 验证外壳是干净的

外壳绝不能携带服务器地址、密钥、设备 token、提示词、布局、编排或 GUI/广告源码。本地即可
证明：

```bash
python3 -m installer.leak_scan                       # exit 0 = 干净
python3 -m unittest installer.tests.test_leak_scan
```

## 跑测试

仅用标准库，无需虚拟环境：

```bash
python3 -m unittest \
  installer.tests.test_install \
  installer.tests.test_shim \
  installer.tests.test_activate \
  installer.tests.test_mcp_config \
  installer.tests.test_leak_scan
```

## 许可

在 [PolyForm Shield License 1.0.0](LICENSE.md) 下源码可见 —— 可用于任何目的，但不得用于
构建一个与之竞争的产品。与 AQG 同一许可。
