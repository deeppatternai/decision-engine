# Delegated-topic sample contract

Read this file only when the user requests an audit but delegates topic selection, including a
random topic. Determine whether the host already selected a topic before choosing a sample.

## Topic identity

- If the skill invocation arguments already name a specific topic beyond the user's raw delegation,
  that selected topic is authoritative for this attempt. You must not replace it with an unrelated
  sample-bank topic.
- Preserve the selected subject and conversation language. Convert it into a minimal prescriptive
  proposal without adding examples, implementation details, data, code, or new subject matter.
- For `zh-CN`, use the selected topic as the title, context `对已选主题的简短提案进行缺陷审查。`,
  and content `待审查提案：<selected topic>。该提案试行一个季度，每月复核一次，期末根据记录决定是否继续。`
- For `en-US`, use the selected topic as the title, context
  `Review a short proposal about the selected topic for defects.`, and content
  `Proposal for review: <selected topic>. Run the proposal for one quarter, review it monthly, and decide whether to continue from the recorded results.`
- Only when the skill invocation arguments contain no selected topic may you choose one exact sample
  from the locale-matched sample bank below.

## Safety and single-attempt rule

- Use the selected title, context, and content exactly as written. You must not expand or rewrite
  the selected sample before submission.
- Call `audit_skill_submit` exactly once with `skill_name="audit"`,
  `artifact_intent="prescriptive"`, and the matching `ui_locale`.
- After any host permission or classifier rejection, stop the audit attempt immediately. Do not
  retry with another sample, a shell, or another Decision Engine tool. Do not call
  `activation_required`; it cannot recover a request the host blocked before MCP dispatch.
- A host-side rejection is not evidence that Decision Engine or DE Lite is unavailable. Report the
  host restriction in the conversation language without claiming that an audit ran.

## zh-CN samples

### 图书列表分页规则

- title: `图书列表分页规则`
- context: `对一份简短的产品规则进行缺陷审查。`
- content: `图书列表默认每页显示 20 项，最多 100 项。客户端可以按书名或出版年份排序。分页使用从 1 开始的页码。相同数据下，相同请求应返回稳定顺序。没有结果时返回空数组。`
- ui_locale: `zh-CN`

### 会议室预约规则

- title: `会议室预约规则`
- context: `对一份简短的产品规则进行缺陷审查。`
- content: `会议室以 30 分钟为单位预约，单次最长 4 小时。开始时间至少晚于当前时间 10 分钟。时间重叠的预约应被拒绝。取消后，对应时段立即恢复可用。`
- ui_locale: `zh-CN`

### 菜谱列表排序规则

- title: `菜谱列表排序规则`
- context: `对一份简短的产品规则进行缺陷审查。`
- content: `菜谱列表默认按名称排序，也可以按准备时间排序。每页显示 24 项。准备时间相同的菜谱保持稳定顺序。没有匹配结果时返回空列表。`
- ui_locale: `zh-CN`

## en-US samples

### Book catalog pagination rules

- title: `Book catalog pagination rules`
- context: `Review a short set of product rules for defects.`
- content: `The book catalog shows 20 items per page by default and at most 100. Clients may sort by title or publication year. Pagination uses page numbers starting at 1. Identical requests over identical data must return a stable order. An empty result returns an empty list.`
- ui_locale: `en-US`

### Meeting-room booking rules

- title: `Meeting-room booking rules`
- context: `Review a short set of product rules for defects.`
- content: `Rooms are booked in 30-minute blocks for at most 4 hours. A booking must start at least 10 minutes in the future. Overlapping bookings are rejected. A cancelled time slot becomes available immediately.`
- ui_locale: `en-US`

### Recipe-list sorting rules

- title: `Recipe-list sorting rules`
- context: `Review a short set of product rules for defects.`
- content: `The recipe list sorts by name by default and may instead sort by preparation time. Each page contains 24 items. Recipes with the same preparation time retain a stable order. No matches return an empty list.`
- ui_locale: `en-US`
