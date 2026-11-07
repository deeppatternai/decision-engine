# Deep Pattern Audit 已安装

Codex Marketplace 使用 `ON_INSTALL` 认证策略。安装时是否复用已有 OAuth 凭据或
重新打开浏览器由 Codex 宿主管理；插件无法读取宿主凭据状态并动态切换安装策略。需要手动重新连接时，请新建会话
运行 setup，或使用宿主提供的 Authenticate。Codex 也可在外层终端执行
`codex mcp login audit`；Claude Code 在交互会话打开 `/mcp`，选择插件提供的
`plugin:audit:audit` 后执行 Authenticate，也可在外层终端执行
`claude mcp login plugin:audit:audit`。Claude 没有 Codex 的 `ON_INSTALL`
认证钩子，安装本身不保证立即打开浏览器。应从发起审计的同一 Code 会话完成认证；
若外层终端 `claude mcp list` 已显示 Connected，而旧会话仍显示 Needs
authentication，请新建 Code 会话继续原请求，不要再次激活或重复提交。
不要从模型执行的 Shell 中运行这些登录命令。安装动作本身不会运行插件钩子，已打开的旧会话也可能继续使用更新前的技能或认证快照。
Claude Cowork 与 Claude Code 的插件目录、会话运行时和 OAuth 凭据域相互独立；
Cowork 需要单独安装 `cowork/` 子目录并新建 Cowork 会话，并从该 Cowork 会话的
Connector 完成 Authenticate/Reconnect。不要用全局 Plugins 页的 Connect 作为 Cowork
已授权证据：宿主可能把该操作路由到当前聚焦的 Code 会话。Code 的 `/mcp` Authenticate
或外层终端登录也不能证明 Cowork 已连接。
宿主会打开 A 的浏览器激活页；
Endpoint 固定显示为 `https://endpoint.deeppattern.ai`，输入管理员提供的激活码并
确认即可。无需重启，也无需安装 Python、Git 或本地 runtime。

激活成功后浏览器回到宿主，OAuth 凭据由宿主安全保存。不要把激活码、access
token、refresh token 或原 DE device token 粘贴到聊天、终端或配置文件中。
未指定审计档次时默认使用 `standard`，无需再次选择。

提交审计后，支持 MCP Apps 的宿主会立即保留运行中卡片，并由卡片自身刷新；Claude
Desktop Code 与 Claude Cowork 会立即给出同一 HTTPS 浏览器入口，并通过有界状态
等待在任务终态后把完整结果返回当前会话。其它不支持卡片的宿主保留浏览器入口。
页面适用于 macOS、
Linux 和 Windows。关闭页面不会取消审核，只有仍在排队且尚未被 worker 接管的任务
可以在页面中停止。
