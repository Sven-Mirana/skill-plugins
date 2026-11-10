# Host-shadow 适配层（默认关闭）

本候选在既有离线路由器外增加最小 host-shadow 适配层。它只生成建议，不替换宿主既有选择、不执行 Skill、不授予权限，也不安装到任何正式宿主。

## 输入边界

适配层拒绝 `prompt`、对话、文件内容和案件事实，只接受两种输入：

1. `redacted_features`：由已复核 index 派生的封闭词表 ID；未知 ID 直接拒绝。
2. `synthetic_fixture`：只允许引用本候选沿用的 39 条合成样本 ID；输出不保存样本文本。

默认配置位于 `host-shadow.json`，其中 `enabled=false`。关闭时适配层不解析 packet，`effective_selection` 与宿主原 `host_selection` 完全一致，且不生成 shadow 建议。

## 开启测试时的边界

- 只有显式测试配置才能令 `enabled=true`；正式随包配置仍保持 `false`。
- 无论 shadow 建议为何，`effective_selection` 始终等于宿主既有选择。
- 输出仅包含建议类别、Skill、确定性分数衍生的置信度、理由码和安全闸标记。
- `confidence` 不是统计概率；其语义固定为 `deterministic_score_ratio_not_probability`。
- 测量 adapter 延迟、输入字节、输出投影字节和 index 字节。
- 因未验证宿主 deferred loading，净 token 节省固定为 `not_measurable`。

## 禁用与回滚

本候选尚未写入正式根，因此当前回滚动作只是停止测试并删除隔离候选；正式 Skill 与旧冻结候选不受影响。若未来获准安装，首先把宿主配置恢复为 `enabled=false` 即可恢复零建议路径；任何 live/default-on、自动执行、宿主 hook 或正式晋升仍需另行冻结、复核和 root 明确批准。

## 合成验证

```bash
python3 scripts/host_shadow_adapter.py evaluate-synthetic \
  --authorized-test \
  --index validation/skill-routing-index.json \
  --fixture fixtures/skill-routing/cases.json \
  --skills-root fixture=fixtures/skill-routing/profiles \
  --output validation/HOST-SHADOW-REPORT.json
```

该报告只能证明 39 条既有合成样本经适配层时保持宿主选择、无执行和无权限扩张；不能证明真实宿主已接入、实际用户提示已观测、fallback 已执行或正式价值成立。
