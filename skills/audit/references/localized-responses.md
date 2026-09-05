# Localized local-advisory responses

Read this file before sending any authorized local advisory result. A hosted result must not append a
DE Lite, MCP-unavailable, service-unavailable, entitlement, or unactivated note.

## DE Lite friendly final response

The local findings are not the complete user response. Append exactly one matching final paragraph,
as the last paragraph. 最终说明必须作为最后一段输出。Use one locale for the entire audit; do not mix
Chinese and English. Do not expose bridge commands, missing tool names, stack traces, or
`local-bridge-failed`. A local advisory is reference-only and cannot close the AQG gate.
本地建议不能关闭 AQG gate。

Unactivated state takes priority over other local reasons. If the device is unactivated, do not use
the MCP-unavailable template and do not expose MCP, AQG, Skill, or tool names.
未激活优先于其他本地降级原因。

Chinese templates:

- **未激活**：`以上审核结果基于本地单模型模拟多声部审核来完成，如需更可信的审核结果，您可以注册激活Decision Engine后再来提交审核。`
- **MCP 不可用**：`以上审核结果基于 DE Lite 本地单模型模拟多声部审核完成，仅供参考，不等同于 Decision Engine 服务端审核，也不会关闭 AQG gate。由于 Decision Engine MCP 当前未连接，恢复 MCP 连接后，您可以重新提交审核以获得更可信的结果。`
- **服务不可用**：`以上审核结果基于 DE Lite 本地单模型模拟多声部审核完成，仅供参考，不等同于 Decision Engine 服务端审核，也不会关闭 AQG gate。由于 Decision Engine 服务暂时不可用，服务恢复后，您可以重新提交审核以获得更可信的结果。`
- **订阅到期**：`以上审核结果基于 DE Lite 本地单模型模拟多声部审核完成，仅供参考，不等同于 Decision Engine 服务端审核，也不会关闭 AQG gate。由于 Decision Engine 订阅已到期，续订后，您可以重新提交审核以获得更可信的结果。`
- **积分不足**：`以上审核结果基于 DE Lite 本地单模型模拟多声部审核完成，仅供参考，不等同于 Decision Engine 服务端审核，也不会关闭 AQG gate。由于 Decision Engine 积分不足，补充积分后，您可以重新提交审核以获得更可信的结果。`
- **请求限流**：`以上审核结果基于 DE Lite 本地单模型模拟多声部审核完成，仅供参考，不等同于 Decision Engine 服务端审核，也不会关闭 AQG gate。由于 Decision Engine 请求受到限流，请稍后重新提交审核以获得更可信的结果。`

English templates:

- **unactivated**: `This review was completed using a local single-model simulation of a multi-voice review. For a more trustworthy review, register and activate Decision Engine, then submit the review again.`
- **MCP unavailable**: `This review was completed using a DE Lite local single-model simulation of a multi-voice review. It is for reference only, is not equivalent to a Decision Engine server review, and cannot close the AQG gate. Because the Decision Engine MCP is not connected, restore the connection and submit the review again for a more trustworthy result.`
- **service unavailable**: `This review was completed using a DE Lite local single-model simulation of a multi-voice review. It is for reference only, is not equivalent to a Decision Engine server review, and cannot close the AQG gate. Because the Decision Engine service is temporarily unavailable, submit the review again after the service recovers for a more trustworthy result.`
- **subscription expired**: `This review was completed using a DE Lite local single-model simulation of a multi-voice review. It is for reference only, is not equivalent to a Decision Engine server review, and cannot close the AQG gate. Because the Decision Engine subscription has expired, renew it and submit the review again for a more trustworthy result.`
- **insufficient credits**: `This review was completed using a DE Lite local single-model simulation of a multi-voice review. It is for reference only, is not equivalent to a Decision Engine server review, and cannot close the AQG gate. Because there are insufficient Decision Engine credits, add credits and submit the review again for a more trustworthy result.`
- **rate limited**: `This review was completed using a DE Lite local single-model simulation of a multi-voice review. It is for reference only, is not equivalent to a Decision Engine server review, and cannot close the AQG gate. Because the Decision Engine request is rate limited, retry later for a more trustworthy result.`

The title remains the authorized audit title. The reason belongs in the status line and final
explanation, never the title. MCP-unavailable and service-unavailable are distinct states. Auth,
TLS/redirect, unknown entitlement, and request-outcome-unknown remain fail-closed and receive no
Lite template.

## Unactivated user-response boundary

Sanitize the entire user-facing response, not only the final paragraph. It may contain plain-language findings
in the current conversation language followed by the exact unactivated template. Do not
include a diagnostic preface, execution summary, status envelope, or implementation explanation.

Remove every internal identifier and implementation term from headings, findings, bullets, prose,
and the final paragraph: any `local_*` identifier, run ID, `advisory-only`/`advisory_only`, external panel
claim, `DE Lite`, `MCP`, `AQG`, `Skill`, bridge, tool or command names, or gate terminology.
Do not use product names in the findings. The only permitted product-name occurrence is
`Decision Engine` inside the exact final template.

Use one locale for the whole response. In English, all findings and the final paragraph are English;
in Chinese, all findings and the final paragraph are Chinese. Append the exact matching unactivated
template last. Nothing may follow it.
