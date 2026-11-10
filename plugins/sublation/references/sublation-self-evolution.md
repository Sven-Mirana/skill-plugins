# Sublation 自我扬弃记录

sublation 是它自己治理的第一个 skill。每一次流程裂缝都必须回流到框架本身，形成新的候选、审计、复核和晋升记录。

## 演进表

| 版本 | 日期 | 触发事件 | 吸收内容 |
|---|---|---|---|
| v1.0 | 2026-05-25 | 初始发布 | 候选、审计、复核、晋升、观察窗的基础框架 |
| v1.1 | 2026-05-28 | 跨 Agent 路径错配 | post_promotion_safety、formal_post_promotion、rollback 证据 |
| v1.2 | 2026-06-02 | 全库上游同步 | sync-before-screen、rollback/backfill、上游 drift 处理 |
| v1.3 | 2026-06-04 | “为扬弃而扬弃”风险 | value_delta_gate，无正向增量不进晋升 |
| v1.4 | 2026-06-10 | Claude Code 首日幻觉 | 自报声明必须用 grep/read-back/diff 硬证据验证 |
| v1.5 | 2026-06-10 | 用户任命 Hermes 为办公室主任 | Hermes 统一汇总、合规确认、群聊管理 |
| v1.6 | 2026-06-11 | 四方治理讨论 | Codex/Hermes/Claude Code 三方独立报告，用户最终决定 |
| v1.7 | 2026-06-11 | 报告刷屏与角色不清 | Hermes 汇总三方报告，用户只看一份简报 |
| v1.8 | 2026-06-11 | NPL 登录墙重复追踪 | MCP 留位模式，登录墙不污染治理视图 |
| v1.9 | 2026-06-11 | 群聊被业务状态污染 | 群聊只承载候选评估、晋升审批、框架修订和宪法变更 |
| v1.10 | 2026-06-11 | 候选层镜像漂移 | 共享根 audit/read-back 护栏，工作区自报不等于候选真相 |
| v2.0 | 2026-06-11 | 系统性重构 | 宪法化、硬边界收束、七章结构、pitfalls 归档、候选共享根一致性 |
| v2.1-candidate | 2026-08-23 | 用户提出“晋升后自动分类并按任务精准适用 Skill” | central router、每 Skill 路由元数据、可重建索引、shadow-only、宿主 adapter fail-open、签名观测与回滚证据 |

## 关键吸收

### 1. 三方治理

单一复核视角会漏实现边界、业务边界或事实核验。现在晋升前必须有 Codex、Hermes、Claude Code 各自的价值/风险报告。

### 2. 观察窗分界

复核通过不等于生产完成。晋升后必须进入 `observation_window`，用真实调用、生产周期或用户确认闭合。

### 3. 登录墙 MCP 预留

登录墙不是缺陷，而是设计约束。保留 MCP/provider contract 接口，不保存凭据，不把 human-login 长期写成阻塞缺陷。

### 4. 幻觉核实

Agent 自报不可信。任何“已完成/已写入/已验证”必须被 read-back、diff、grep、hash、audit 或 fixture 支撑。

### 5. 路径强校验

manifest path 不是事实。跨 Agent 根、source/target 不一致、正式路径未被修改，必须由 filesystem-derived check 捕获。

### 6. 价值增量门

候选通过 audit 只表示结构可审，不表示值得晋升。正向增量、旧能力保留、fallback、观察窗验收标准必须写清。

### 7. 统一汇报

三方仍独立评估，但最终呈报用户的晋升建议由 Hermes 汇总。这样用户只看一份去重、标注分歧、带阻塞项的简报。

### 8. 候选共享根

候选工作区不是事实源。跨 Agent 修复必须同步到 `~/.hermes/sublation/candidates/`，并在共享根复跑 audit/read-back。否则 Codex、Hermes、Claude Code 可能基于不同副本得出互相矛盾的结论。

### 9. 晋升后分类路由候选

#### 9.1 触发事实

现有 Skill 目录名和物理安装根不等于业务领域；同一法律、金融、通用写作或排版任务可能跨多个根。旧 scenario map 是人工目录，宿主完整 catalog 又可能带来选择噪声，因此需要一个 central router 用结构化元数据缩小候选集。但“更精准、更省 token”只是待检验假设，不是提案本身可以证明的事实。

#### 9.2 被吸收的原则

- 每个 Skill 的 `routing.json` 是该 Skill 分类事实源；全局 taxonomy 管词表，central router 归 `skill-sublation` 单点持有，派生索引可删除重建且不能反写元数据。
- 自动分类先产生 `suggested`，经人工和独立复核才能成为 `approved`；`routing.json` 与 manifest `routing` 必须成对绑定，不能删块逃逸。
- 领域只作粗排先验；能力可多选；显式点名、正负触发、澄清条件、跨域组合和 high-risk safety signal 共同决定建议。
- 本候选只允许 shadow：router 不调用 Skill、不改变宿主实际选择、不扩权；schema 中的 live 兼容值不是启用入口。
- `fallback` 是控制信号，不是已执行行为。Hermes、Claude Code、Codex 等宿主各自需要 adapter 捕获错误并真正回到原 catalog，同时继续执行宿主启停、平台、依赖、隔离、优先级、权限和安全规则。

#### 9.3 必须保留的矛盾

精准筛选可能造成召回损失；自动建议可能把启发式推断冒充正式事实；可移植元数据无法代表不同宿主的 discovery、namespace、权限和运行可用性；集中索引提高一致性，也形成需要 hash、绑定和回滚保护的单点。处理方式不是隐藏矛盾，而是在 shadow 中与旧选择并排观察并保持旧 catalog 不变。

#### 9.4 证据与闭环

离线 `synthetic_fixture` 只证明给定索引和输入下的确定性、排序和反例行为，不等于宿主集成、真实 fallback、token 节省或运行闭环。观察窗关闭必须按宿主分别取得 `authorized_shadow_runtime` 样本，并绑定：准确目标与 hash 的签名授权收据、经本地 key 复验的 HMAC 链式脱敏日志及验证收据、同一 index/router/profile 的签名 rollback 事件、旧 catalog 未变证据和独立复核。授权收据、签名日志和回滚事件用途不同，不能互相替代。

需要观察的假设至少包括：高风险误路由和权限扩张为零；显式点名遵循率、确定性和真实 legacy fallback 成功率为 100%；普通准确率不低于旧基线且人工改选率不升；P95 附加延迟可接受；只有宿主按需加载被独立证明且扣除 router/adapter 开销后，才可声称净 token 节省。

#### 9.5 延期与回滚

本候选不包含全量自动重分类、在线学习、远端遥测、自动执行、宿主 adapter 安装、live/default-on 或收益结论。元数据晋升、adapter 安装和 live 激活是三个不同的授权面；后两者必须另建候选并单独获得用户批准。任何错误、漂移、超时、签名验证失败或指标回归都保持/恢复旧 catalog，撤掉派生索引和 adapter 配置，并用绑定的 rollback point 与签名 journal 证明恢复结果。

因此，v2.1 当前只是共享候选中的 shadow 设计与实现，不是正式晋升、宿主安装、真实调用完成或价值验证结论。

## 循环

```text
经验暴露裂缝 -> 建候选修框架 -> 三方复核 -> 用户批准
-> 晋升观察 -> 新经验暴露新裂缝
```

这个循环不能只用于其他 skill。sublation 的可信度来自它自己也被同一循环治理。
