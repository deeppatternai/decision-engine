"""Chinese (zh-CN) UI strings. English lives in `en_US.py` — never mix languages in one file.

Every table here has a key set identical to its `en_US.py` twin (a parity test recurses through all
tables and guards against drift). `%(name)s` / `%s` placeholders are filled by the caller with `%`.
Nothing here decides language; `client.i18n` does.
"""

# GE terminal-notice pages (client/popup/notice.py).
NOTICE = {
    "close": "关闭",
    "failed": ("图解生成失败", "服务端未能完成这次图解生成。请稍后重试。"),
    "cancelled": ("图解生成已取消", "这次图解任务已取消，没有可显示的产物。"),
    "pending": ("图解仍未完成", "自动等待已结束。你可以稍后使用运行编号恢复查看。"),
}

# GE follow-up-chat RUNTIME strings (client/popup/chat_backend.py). See en_US.py for the contract note.
CHAT_DEFAULTS = {
    "meta_refusal": "这部分我无法告知～我只负责帮你把左边这张图讲清楚。图里有什么想深入了解的吗?",
    "timeout": "这次追问超时了——没能及时拿到回复,要再试一次吗?",
    "error": "追问失败——出了点内部错误,请再试一次。",
    "model_error": "追问失败——这次没能生成回复,请再试一次。",
    "unavailable": "追问功能暂时不可用,请稍后再试。",
    "missing_claude_cli": (
        "当前设备未安装 Claude Code CLI，因此无法使用右侧追问。"
        "请在终端安装并完成登录，然后重新打开此窗口。"),
    "missing_codex_cli": (
        "当前设备未安装 Codex CLI，因此无法使用右侧追问。"
        "请在终端安装并完成登录，然后重新打开此窗口。"),
    "missing_cursor_cli": (
        "当前设备未安装 Cursor Agent CLI，因此无法使用右侧追问。"
        "请在终端安装并完成登录，然后重新打开此窗口。"),
    "cursor_followup_disabled": (
        "Cursor Agent 右侧追问已被配置禁用。请启用后重新打开此窗口。"),
    "images_unsupported": "Cursor 追问暂时不支持图片附件，请改用文字提问。",
}

# First-use device-activation form (client/popup/launcher.py :: render_activation_html).
ACTIVATION = {
    "secret_label": "授权码",
    "name_label": "设备名（可选）",
    "submit": "激活",
    "cancel": "取消",
}

# Stopper audit-list panel (client/stopper/panel.py). See en_US.PANEL for the honesty invariant. The
# de_lite lines say「仅供参考」and the local_line lines say「仅参考」— that asymmetry is intentional
# and pinned by tests; do not "normalize" it. Only de_lite failed carries ✗; local_line failed does not.
PANEL = {
    "empty": "没有进行中的审核",
    # 面板静态框架文案（与 `empty` 一样按宿主 locale 解析）：系统窗口标题、每行的审核编号列标签、
    # 以及某个 run 自身 title 为空时的兜底标题。`window_title` 为产品名，不译。
    "window_title": "Decision Engine",
    "id_label": "编号",
    "default_title": "审核",
    "local_fallback_label": "🔶 本地降级",
    "cross_vendor": "跨厂商审核",
    "tier": {"fast": "快速", "standard": "标准", "deep": "深度"},
    "action": {
        "cancelling": "停止中", "cancelled": "已取消", "stop": "停止",
        "running": "进行中", "checking": "检查中", "finished": "已完成",
    },
    "reason": {
        "unactivated": "未激活", "subscription_expired": "订阅到期",
        "credits_exhausted": "积分不足", "rate_limited": "服务限流",
        "service_unavailable": "服务不可用", "mcp_unavailable": "MCP 不可用",
    },
    "de_lite": {
        "queued": "DE Lite · 本地审核排队中%s ⏱",
        "running": "DE Lite · 本地审核中%s · %s ⏱",
        "cancelling": "DE Lite · 本地审核停止中%s · %s ⏱",
        "cancelled": "DE Lite · 本地审核已取消%s · %s",
        "failed": "DE Lite · 本地审核失败%s · %s ✗",
        "completed": "DE Lite · 本地审核完成（仅供参考）%s · %s",
        "partial": "DE Lite · 本地审核部分完成（仅供参考）%s · %s",
        "unknown": "DE Lite · 本地审核状态未知（仅供参考）%s ⏱",
    },
    "local_head": "🔶 本地降级 · 单模型非跨厂商",
    "local_line": {
        "queued": "%s · 排队中 ⏱",
        "running": "%s · 本地审核中 · %s ⏱",
        "cancelling": "%s · 停止中 · %s ⏱",
        "cancelled": "%s · 已取消 · %s",
        "failed": "%s · 失败 · %s",
        "completed": "%s · 完成（仅参考） · %s",
        "partial": "%s · 部分完成（仅参考） · %s",
        "unknown": "%s · 状态未知（仅参考） ⏱",
    },
    "hub_stale": "%s · 连接中断 · 最后已知 %s ⚠",
    "hub_queued": "%s · 排队中 ⏱",
    "hub": {
        "completed": "%s · 已完成 · %s ✓",
        "partial": "%s · 部分完成 · %s ⚠",
        "failed": "%s · 失败 · %s ✗",
        "cancelling": "%s · 停止中 · %s ⏱",
        "cancelled": "%s · 已取消 · %s",
        "reviewing": "%s · 审核中 · %s ⏱",
    },
    "auditor_status": {
        "queued": "\u6392\u961f\u4e2d",
        "pending": "\u5f85\u5904\u7406",
        "running": "\u5ba1\u6838\u4e2d",
        "completed": "\u5df2\u5b8c\u6210",
        "partial": "\u90e8\u5206\u5b8c\u6210",
        "failed": "\u5931\u8d25",
        "cancelling": "\u505c\u6b62\u4e2d",
        "cancelled": "\u5df2\u53d6\u6d88",
    },
}

# GE follow-up-chat page-side JS strings (launcher._GE_CHAT_JS). See en_US.GE_CHAT for the injection model.
GE_CHAT = {
    "send": "发送",
    "retry": "重试",
    "free": "免费",
    "credits": "积分",
    "image": {
        "dialog": "所选图片预览", "close": "关闭图片预览",
        "selectedPrefix": "第 ", "selectedSuffix": " 张所选图片",
        "viewPrefix": "查看第 ", "viewSuffix": " 张图片大图",
        "sentPrefix": "第 ", "sentSuffix": " 张已发送图片",
        "sentViewPrefix": "查看已发送的第 ", "sentViewSuffix": " 张图片大图",
        "removePrefix": "移除第 ", "removeSuffix": " 张图片",
    },
    "error": {
        "capability_disabled": "追问功能已关闭。",
        "server_unsupported": "当前服务端不支持追问。",
        "client_unsupported": "当前客户端不支持该操作。",
        "auth_required": "请从宿主重新打开此图后继续追问。",
        "privacy_confirmation_required": "当前服务端配置无法使用追问。",
        "invalid_request": "请检查问题和图片后重试。",
        "input_too_large": "问题或图片过大。",
        "turn_in_flight": "另一条追问仍在处理中。",
        "conversation_busy": "当前会话正忙，请稍后重试。",
        "conversation_expired": "当前追问会话已过期。",
        "rate_limited": "请求过于频繁，请稍后重试。",
        "insufficient_credits": "当前积分不足，无法追问。",
        "model_unavailable": "模型暂时不可用。",
        "timeout": "追问超时，请重试。",
        "init_timeout": "初始化超时，请重试。",
        "cancelled": "追问已取消。",
        "network_error": "网络不可用，请重试。",
        "bad_response": "追问服务返回了无效响应。",
        "not_found": "当前追问会话已不可用。",
        "chat_unavailable": "追问暂不可用。",
    },
    "state": {
        "capability_checking": "正在检查可用性", "unavailable": "追问暂不可用。",
        "creating_conversation": "正在开始追问", "restoring_history": "正在恢复历史",
        "ready": "就绪", "submitting": "正在发送", "queued": "正在排队", "running": "正在处理",
        "calling_model": "正在处理", "completed": "已完成", "failed": "追问失败。",
        "cancelled": "已取消", "recovering": "连接中断，请重试",
    },
}

# GE header/region-chrome page-side JS strings (launcher._GE_CHROME_JS). Same injection model as GE_CHAT.
GE_CHROME = {
    "shot_ok": "已截图到剪贴板",
    "shot_na": "截图仅在弹窗中可用",
    "shot_err": "截图失败，请重试",
    "shot_uns": "此环境暂不支持截图到剪贴板",
    "share_na": "分享仅在弹窗中可用",
    "share_err": "分享失败，请重试",
    "share_uns": "此环境暂不支持分享",
    "arm": "拖一个框，就框住的那块图追问",
    "reg_na": "框选提问仅在弹窗中可用",
    "reg_err": "截取选区失败，请重试",
    "reg_uns": "此环境暂不支持框选提问",
    "reg_add": "已加到追问，输入问题即可发送",
    "reg_reject": "图片已达上限（最多5张），未添加",
}

# 原生窗口外壳 —— 无边框标题栏控件（Windows chrome）与 macOS 菜单栏托盘 tooltip。
# tray_toggle 通过 %s 接收窗口标题。
SHELL = {
    "maximize": "最大化窗口",
    "restore": "还原窗口",
    "tray_toggle": "%s（点击：显示／隐藏）",
    "surface_title": {
        "audit": "Decision Engine - \u5ba1\u8ba1",
        "diagram": "Decision Engine - \u56fe\u89e3",
        "comic": "Decision Engine - \u6f2b\u89e3",
        "infographic": "Decision Engine - \u4fe1\u606f\u56fe",
        "discussion_board": "Decision Engine - \u8ba8\u8bba\u677f",
    },
}

# 由 Agent 拉起的设备永久激活表单（installer/permanent_setup.py）。见 en_US.PERMANENT_SETUP 的契约说明：
# 一张表同时供 pywebview HTML、tkinter 回退和稳定的顶层结果文案使用，`title` 为产品名（两种语言刻意一致），
# `window_title` 是系统标题栏文案；错误/结果文案均为本方自撰，绝不回显服务端返回值，按稳定 slug 查表。
PERMANENT_SETUP = {
    # <html lang> 子标签：驱动 webview 的中日韩字体选择与读屏发音；不是解析器的 "zh-CN" locale 标签。
    "html_lang": "zh-Hans",
    "window_title": "Decision Engine 永久配置",
    "title": "Decision Engine",
    "subtitle": "将此设备连接到你的工作空间",
    "endpoint_label": "Owner 提供的 endpoint",
    "endpoint_hint": "请填写 Decision Engine owner 提供的 HTTPS 地址。",
    "secret_label": "激活密钥",
    "secret_hint": "仅用于本次激活，不会保存在此设备上。",
    "secret_placeholder": "请输入激活密钥",
    "cancel": "取消",
    "activate": "激活",
    "activating": "正在激活…",
    "setup_failed_title": "Decision Engine 配置失败",
    "setup_recovery_title": "Decision Engine 配置需要恢复",
    "setup_success_title": "Decision Engine 永久配置",
    "setup_succeeded": (
        "永久配置成功。激活密钥未被保留。"
        "请完全重启 Agent；后续重启无需重复输入。"
    ),
    "already_activated_success": (
        "此设备已永久激活。MCP 接线和 Doctor 已就绪。"
        "请完全重启 Agent 以加载 Decision Engine。"
    ),
    "success_with_host_failures": (
        "永久配置已为其他 Agent 客户端完成。"
        "这些客户端仍需修复：%(failed_hosts)s。请完全重启已配置成功的 Agent。"
    ),
    "doctor_failure": (
        "设备凭据和 MCP 接线已保存，但 Doctor 仍报告失败。"
        "请修复终端输出中报告的问题，然后重新运行永久配置。"
    ),
    "activation_failed_safe": "激活失败，请检查 endpoint、网络与激活密钥后重试。",
    "activation_incomplete": "激活未完成，请核对输入后重试。",
    "activation_recovery_required": (
        "服务可能已经接受了此设备，但本地凭据未能保存。"
        "请保留当前安装，并使用 Owner 指导的恢复流程。"
    ),
    "unexpected_setup_failure": (
        "配置未能完成。未显示任何凭据详情。请查看终端输出，"
        "修复报告的问题后重试。"
    ),
    "errors": {
        "endpoint_invalid": "请输入有效的 HTTPS endpoint。",
        "endpoint_too_long": "Endpoint 过长。",
        "endpoint_required": "请填写 Endpoint。",
        "secret_invalid": "请输入有效的激活密钥。",
        "secret_too_long": "激活密钥过长。",
        "secret_required": "请填写激活密钥。",
        "secret_rejected": "激活密钥无效或已过期，请核对后重试。",
        "rate_limited": "激活尝试过于频繁，请稍候片刻后重试。",
        "rejected": "激活被拒绝，请核对 endpoint 与激活密钥。",
        "service_unavailable": "激活服务暂时不可用，请稍后再试。",
        "activation_failed": "激活失败，请检查 endpoint、网络与激活密钥后重试。",
        "activation_incomplete": "激活未完成，请核对输入后重试。",
        "in_progress": "已有一个激活流程正在进行。",
        "bridge_error": "无法完成激活，请核对输入后重试。",
        "generic": "请核对输入后重试。",
    },
}

# MCP 工具描述（installer/shim.py）。shim 拥有每个工具的结构（name / inputSchema 类型 / enum /
# required）；本表只承载它在 tools/list 组装时叠加的、面向人的 `description` 与各参数描述。键为线上
# 工具名，不翻译；enum 值 / 数字上限 / {status:...} 等 token 保留在 schema 里，绝不出现在这里。
# 与 en_US.MCP_TOOLS 形状一致（parity 测试递归校验）。
MCP_TOOLS = {
    "activation_required": {
        "description": "在使用 Decision Engine 工具前，请先激活此设备。",
        "params": {},
    },
    "audit_skill_submit": {
        "description": "在当前交互式 agent 会话中启动一次仅供参考的 DE Lite 本地缺陷审查。此操作不会联系 Decision Engine Hub。",
        "params": {},
    },
    "audit_skill_complete": {
        "description": "结束当前的 DE Lite 本地审查。此操作只更新本地审核面板状态，不会联系 Decision Engine Hub，也绝不会关闭审核闸门。",
        "params": {},
    },
    "service_unavailable": {
        "description": "无法连接 Decision Engine Hub。请重新连接后重试。",
        "params": {},
    },
    "open_ge_popup": {
        "description": "在用户机器上的原生弹窗中打开服务端渲染的图解产物（通过 run_id）。返回 {status, popup_id}。图片字节在客户端获取，绝不会返回给你 — 不要内联。",
        "params": {
            "run_id": "visual_render 的 run_id。",
            "title": "简短的窗口标题（可选）。",
            "context": "弹窗内追问聊天的对话上下文（可选；slice ③）。",
        },
    },
    "open_ge": {
        "description": "用一次调用在原生弹窗中打开服务端渲染的图解：对 comic、infographic 或 diagram，此工具会提交渲染、等待完成、在客户端获取成品产物并打开弹窗 — 你无需轮询或调用 open_ge_popup。打开后返回 {status, popup_id}；产物字节绝不会返回给你 — 不要内联。此调用最多等待 10 分钟以获取服务端的终态状态。仅当超过该上限后运行仍处于 pending 时，才返回 {status:'scheduled', run_id} 并在一个有界的尽力而为 worker 中继续。若提交结果未知，返回 {status:'request_outcome_unknown', retryable:true, client_request_id}；只能用完全相同的 client_request_id 重试。提交之后，任何失败结果都会包含 run_id 以便诊断与恢复。",
        "params": {
            "mode": "服务端渲染模式；每种模式都走一次调用的 open_ge 流程。",
            "spec": "该模式的用户内容规格 — 参见 visual_render 工具的 inputSchema。",
            "client_request_id": "可选的 DE 幂等键；首次使用前会去除首尾空白。若提交结果不确定，请复用响应返回的、完全规范化后的值。",
            "title": "简短的窗口标题（可选）。",
            "context": "弹窗内追问聊天的对话上下文（可选）。",
        },
    },
    "open_db_board": {
        "description": "将讨论板（用户内容规格）作为可交互的原生弹窗打开以供手动编辑。返回 {status, popup_id}；轮询 db_board_result 获取编辑后的板。",
        "params": {
            "spec": "板的规格（列/便签等）— 仅限用户内容。",
            "title": "简短的窗口标题（可选）。",
        },
    },
    "db_board_result": {
        "description": "对讨论板弹窗的结果进行有界轮询（≤55s）。返回 {status:'open'}（再次轮询）、{status:'done', board}（用户的编辑）、{status:'dismissed'} 或 {status:'unknown'}。在过期之前，该板都可通过 popup_id 取回。",
        "params": {
            "wait_s": "最长阻塞秒数（默认 50，上限 < 60）。",
        },
    },
}

# 与 en_US.MCP_ERRORS 一一对应（parity 测试递归校验）。
# ``activation_required:`` 前缀是协议标记，逐字保留、不翻译；产品名 Decision Engine 同样保留。
MCP_ERRORS = {
    "service_unavailable": "Decision Engine 服务不可用 — 请重新连接后重试",
    "activation_required": (
        "activation_required: 在使用 Decision Engine 工具前，请先激活此设备"
    ),
}
