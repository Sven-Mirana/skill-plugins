# Batch Sublation Screening Workflow

当用户要求批量筛选 skill 做 sublation 扬弃时，标准流程：

## 前置：上游同步

在筛选之前，必须先同步所有上游源仓库。`hermes skills update` 对 local-source skill 返回 No updates 不代表最新。

```bash
# 1. 识别所有源仓库（查 memory + 各 skill 的 SKILL.md 中 github.com URL）
# 2. git clone --depth 1 到 ~/Desktop/skill文档夹/
# 3. diff -rq 对比上游 vs 本地（排除 __pycache__/.pyc/.DS_Store）
# 4. 报告差异
```

已知源仓库（2026.7.1）：
- JimLiu/baoyu-skills → creative/baoyu-*
- cat-xierluo/legal-skills → legal/
- lijigang/ljg-skills → ljg/
- JimLiu/canghe-skills → ❌ 仓库已删，无法同步

## 全库扫描

```bash
# 1. 列出所有已安装 skill
find ~/.hermes/skills -name 'SKILL.md' -maxdepth 4 | sort

# 2. 按维度分类：
#    - 已 sublated（有 PORT_NOTES.md）
#    - 已归档（.archive/）
#    - 含脚本（scripts/*.py）
#    - 纯文档（仅 SKILL.md）
```

## 三梯队分级

| 梯队 | 条件 | 优先级 |
|------|------|:--:|
| 第一梯队 | 有脚本 + 外部 API + 已知改进点 | 最高 |
| 第二梯队 | 有脚本但无明显问题 | 中 |
| 第三梯队 | 纯文档/prompt | 跳过 |

## Claude 对表

1. Hermes 全库扫描 → 三梯队分级 → 发群聊
2. Claude Code 独立视角扫描 → 给出自己的候选排序
3. 两人对表 → 合并去重 → 出统一批处理清单

## 喂给 Loop v3

等 Codex 上线后，按统一清单逐批喂给 Loop Engineering v3 automation：

```bash
python3 scripts/loop_engineering.py run \
  --candidate <candidate-path> \
  --output-dir validation/loop-v3 \
  --no-room
```

自动化默认终点是 `USER_DECISION_REQUIRED`，不会自动晋升。用户审批后再进入下一批。

## 防过早收敛

- 不要一轮筛选就说"只有 N 个值得做"
- 用户说"多筛选几批"就要继续放宽标准
- 每批结束后汇报进度，确认是否继续
