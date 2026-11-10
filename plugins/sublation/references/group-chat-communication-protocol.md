# 四方群聊通信协议

## 语言铁律

群聊桥所有消息必须使用**纯中文**。技术术语（如 sha256、audit、candidate）可以用英文，但整句必须是中文。

## 禁止

- ❌ 英文句子混入中文消息
- ❌ 拼音替代中文（如 "ni hao" 代替 "你好"）
- ❌ 中英混排句子（如 "这个 candidate 已经 passed 了"）
- ❌ Hermes 用英文跟 Codex/Claude Code 对话

## 允许

- ✅ 技术术语保留英文：`sha256`、`audit`、`manifest`、`PATCH.diff`
- ✅ 路径保留原样：`~/.hermes/sublation/candidates/`
- ✅ 代码块和命令保留原样

## 背景

用户明确说过"你又不是外国模型"，要求四方群聊用中文。6.22 夜场 Hermes 在群聊中使用了英文消息和拼音混写，已被指出。此后再犯即为重复违规。

## 协调消息纪律

四方群聊不是简单转述通道。任何 agent 收到 task-flow 变化、`@codex`、`@claude`、`@hermes` 或用户直接指令时，先判断这条消息要求的是：提出方案、执行候选、独立复核、统一报告，还是转达用户已经作出的决定。

Hermes 作为默认 coordinator 时，消息职责是“方向建议 + 成果复核 + 统一报告”。她可以提醒补证据、归并分歧、请求 Codex/Claude Code 核盘，但不得只把某个 agent 的一句话原样转成结论，也不得把“Claude 说可以”“Hermes 同意”“Codex 建议”写成 用户 授权。

涉及以下持久化或高风险动作时，群聊结论只能写成方案或 `USER_DECISION_REQUIRED`，除非 用户 已在当前 scope 中明确批准：

- formal skill 写入、晋升、回滚；
- PATH、symlink、alias、launchd、cron、MCP、provider 配置变更；
- credential、login、session、外部发布；
- live validation、live scraping、download；
- optimizer、iterative、sync pull、load_skill。

如果证据已经足够但授权不足，正确消息形态是：列出证据、给出建议方案、标注等待 用户 yes/no。不能用“你执行吧”“大家同意了”替代用户批准。

## 自查方法

发消息前逐条检查：
1. 这句话的主体语言是中文吗？
2. 有没有整句英文？
3. 有没有拼音代替中文词？

如果答案不全是"是/没有/没有"，重写。

## 消息长度和格式铁律

群聊消息必须短、精、可操作。用户已多次手动 block 超长消息（2026-07-06 两次）。

| 禁止 | 原因 |
|------|------|
| 超过 500 字的汇报 | 群聊不是报告通道，长报告写到文件然后用群聊通知路径 |
| 贴原始输出（表格、JSON、堆栈跟踪） | 数据放文件，群聊只说结论 + 路径 |
| 复述别人的分工 | "Codex 说了 1. akshare 2. cn-financial..." → 用户不需要传话筒 |
| 逐项重复已有群聊内容 | 如果群聊里已经讨论过，只说"收到，执行" |
| 贴实测数据作为消息主体 | 实测数据写到桌面文件，群聊引用文件路径 |

正确格式：
- 每条消息 ≤300 字
- 结构：结论 + 文件路径 + 下一步
- 需要讨论的问题：单句提问
- 被 block 后：缩短重写，不要重发相同内容

## 相关

- skill-sublation §3.7 群聊桥
- memory: "四方群聊中文铁律"
