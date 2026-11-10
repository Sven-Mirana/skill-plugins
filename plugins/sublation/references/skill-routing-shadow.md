# Central Router 与 Shadow 路由

本文件描述当前候选中的晋升后分类与路由画像。它只负责缩小候选集、给出可解释建议和收集去标识观测，不能执行 Skill、授予权限、安装宿主 hook、切换默认路由或替代用户的晋升决定。

当前状态是 `shadow-only`。本候选提供 central router 的库/CLI 与治理门禁，但没有接入 Hermes、Claude Code、Codex 或其他宿主的 discovery/loading/turn hook；离线命令成功不代表宿主已经使用它。

## 权威与身份边界

- 每个 Skill 根内的 `routing.json` 是该 Skill 路由事实的权威来源，并随 Skill 树一起哈希、审计和回滚。
- `references/skill-routing-taxonomy-v1.json` 只管理全局领域、能力词表和阈值；`references/scenario-routing-migration-v1.json` 只给旧 scenario ID 提供迁移建议，不能批准分类。
- central router 由正式 `skill-sublation` 单点持有和版本化。普通 Skill 只携带 `routing.json`，不得私带、分叉或覆盖 router。
- `skill_router.py index` 生成的是可删除重建的治理路由画像，不是任何宿主的 active catalog，禁止反向覆盖 `routing.json`。
- 索引使用 `root_id:relative_path` 作为运行画像身份，并绑定 declared name、Skill tree、metadata、profile set、taxonomy 和 router hash。显式 `--metadata` 产生的合成身份只适合 fixture，不是可持久化的生产身份。
- 索引不表达宿主全部 disabled、platform、requirements、quarantine、project/org precedence、plugin namespace、alias/collision 或可用性事实；这些事实仍由宿主掌握。

## Shadow-only 数据流

```text
已授权的提示词副本 -> host adapter -> central router -> advisory result
宿主原 active catalog -----------------------------> 实际选择/执行
router fallback signal -> host adapter -> legacy catalog
```

shadow 期间，router 结果只与宿主原选择并排比较，不能替换原选择。router 的所有正常结果都必须保持 `advisory_only=true`、`execution_performed=false`、`authority_granted=false`；live index 必须被拒绝。schema 或内部函数中的 `live` 值只是未来兼容位，不是当前 CLI 或运行许可。

## Gate 0：先量宿主基线

```bash
python3 scripts/skill_router.py baseline \
  --skills-root ~/.claude/skills \
  --host-loading-mode unknown
```

`unknown` 或 `full-injection` 都不得宣称节省上下文。只有宿主已经被独立证明支持按需加载，才能使用 `deferred`，并继续测扣除 router、index 和 adapter 开销后的净 token 与延迟。每个宿主要分别建立基线，不能把一个宿主的结果外推给另一个。

## 候选元数据

新候选由 `candidate.py` 生成 `routing.json` 建议。自动建议只读取名称和 frontmatter 描述，`classification.status=suggested` 不具权威。人工与独立复核至少检查：

- 一级领域是否稳定，能力是否可多选；
- 正触发与负触发是否覆盖中英文、同词异域和明确不适用反例；
- I/O、依赖、risk tier、允许/禁止动作是否真实；
- 明确点名只影响排序，不绕过权限、安全、依赖或宿主可用性检查；
- `fallback_mode` 必须是 `legacy_catalog`；
- `routing.json`、同目录 `SKILL.md` 名称和 manifest `routing` block 是否绑定同一身份与 hash。

分类从 `suggested` 到 `approved` 必须有人工复核证据。post-cutover 候选不得通过删除 `routing.json` 或漏写 manifest `routing` block 绕过门禁。

## 构建与离线检查

```bash
python3 scripts/skill_router.py index \
  --skills-root <root-id>=<candidate-or-formal-root> \
  --mode shadow \
  --output /tmp/skill-routing-index.json

printf '%s' '<合成或已授权提示词>' | \
  python3 scripts/skill_router.py route \
    --index /tmp/skill-routing-index.json \
    --skills-root <root-id>=<same-runtime-root> \
    --prompt-file -
```

真实 shadow 观测必须传相同 `--skills-root` 以复验运行时绑定；`runtime_binding_verified=null` 只能做离线探索，不能关闭观察窗。`route` 不调用推荐 Skill，也不检查宿主执行权限。

真实观测必须先生成并冻结脱敏计划，再取得绑定该计划的授权，最后才允许写观测。HMAC key 必须是候选目录外部的 0600 普通文件：

```bash
python3 scripts/skill_router.py plan \
  --index /tmp/skill-routing-index.json \
  --fixture <authorized-shadow-fixture.json> \
  --attestation-key <external-0600-key> \
  --output <redacted-fixture-plan.json>

python3 scripts/skill_router.py evaluate \
  --index /tmp/skill-routing-index.json \
  --fixture <authorized-shadow-fixture.json> \
  --skills-root <root-id>=<same-runtime-root> \
  --attestation-key <external-0600-key> \
  --plan <redacted-fixture-plan.json> \
  --observation-log <new-events.jsonl> \
  --output <report.json>
```

`plan` 只验证 index/fixture/key 并写入不含原始提示词的计划，不执行 route，也不写 observation。`evaluate` 在任何 route 或日志写入前，以同一 index、fixture 和 key 重算并逐字段、规范哈希比对已有计划；缺失、篡改、换 key 或软链接计划一律拒绝且不创建日志。旧 `--plan-output` 仅保留给无 observation 的 `synthetic_fixture` 离线检查，不能代替真实 shadow 的预存计划。

## Fallback 只是信号

`decision=fallback`、`fallback_used=true` 和 `fallback_target=legacy_catalog` 只表示“请宿主回到旧路径”。即使 CLI 对缺失、损坏或不可读索引输出结构化 fallback JSON，它也没有调用旧 catalog。超时、进程崩溃、输出解析失败等宿主侧故障更不可能由该结果自行恢复。

每个宿主 adapter 必须：

1. 从宿主原生 active catalog 解析可用 Skill，并给 central router 提供稳定 root/runtime 绑定；
2. 把 router 建议与该 active catalog 求交集，继续执行宿主自己的启停、平台、依赖、隔离、优先级、命名冲突、权限和 safety 规则；
3. 在 shadow 中保留宿主原选择为实际选择，只把建议和 baseline 做比较；
4. 捕获索引缺失/损坏/漂移、taxonomy/router 不可用、低置信、超时、崩溃及输出协议错误，并实际调用原 legacy catalog；
5. 不把 prompt、对话、文件或案件内容写入观测，只传必要的瞬时输入并记录不透明 request ID 和脱敏结果字段。

本候选没有安装上述 adapter。因此当前可以验证 router 信号，不能声称真实 fallback 成功。真实闭环还必须绑定候选树外普通文件形式的 adapter ID/path/hash、`skill-sublation.routing-fallback-execution-receipt.v1` 收据、逐案不透明 ID 与同一签名 journal 中的 `routing_host_fallback_execution_verified` 事件；router 报告字段不能替代这些宿主证据。adapter 安装/配置写入必须另建候选、复核准确宿主边界并取得用户明确批准。

## 决策规则

1. `primary_domain` 仅是粗排先验，不能成为硬闸门。
2. triggers、negative_triggers、capabilities 和显式 Skill 名共同形成可解释证据；载体/文种等宽泛词不得代替实质任务触发词，明确的“不做某类分析”必须进入负触发回归。
3. 推荐数组必须先剔除所有低于最小分数的候选；没有合格候选时发出 legacy catalog fallback 信号，跨域合格候选接近时输出 `compose_or_clarify`。
4. 明确点名优先，但命中负触发时仍输出 `clarify`；高风险只标记 safety gate，不自动执行。
5. shadow 索引可纳入 candidate 画像，但始终只读建议；当前发布拒绝 live 路由。

## 授权、签名日志与回滚证据

路由治理必须区分四类证据：

1. **晋升授权收据**：绑定 exact candidate、PATCH、formal baseline 和批准范围，只授权元数据/central router 的该次晋升。
2. **Shadow 观测授权收据**：在观测前由可信 run 产生，绑定 candidate ID、shadow index hash、预提交 HMAC key ID/hash 和 `mode=shadow`。
3. **签名观测日志及验证收据**：日志使用 0600 本地 key、事件 hash 链和 HMAC；`verify-log` 必须重新验证链、签名、隐私标志、request ID 唯一性及 index/router/profile 绑定。它证明日志完整性，不证明用户授权、路由正确或 fallback 已执行。
4. **宿主 fallback 执行收据**：绑定候选树外 adapter 哈希、报告中的 fallback 案例集合、不透明 case ID、旧 catalog selection ID 和完成结果，并进入同一签名 journal。
5. **签名回滚事件**：同一 run journal 必须绑定相同 candidate/index/router/profile，并在宿主 fallback 事件之后证明 registry rollback 可用、legacy catalog 未被改写。

授权收据、签名日志和回滚证据不能互相替代。任何 hash、key、时间、身份或 journal 链不一致都必须 HOLD。路由元数据晋升也不授权宿主 adapter 安装或未来 live/default-on；这两个动作各自需要新候选、用户批准和独立 rollback point。

## Observation Gate

- `synthetic_fixture`、单元测试或离线 CLI 只证明确定性和反例行为，不等于运行闭环，也不能填报为 `authorized_shadow_runtime`。
- 闭合必须有用户授权后的真实宿主 shadow 样本、可复验 runtime binding、签名日志与收据、真实 legacy fallback/rollback 演练和独立复核；至少 30 个有效样本并满足规定 cohort。
- 高风险误路由、权限扩张、原始提示词泄漏和 tree-drift 漏检均为 0；任一出现即 HOLD。
- 显式点名遵循率、确定性排序率和真实 fallback 成功率必须为 100%；普通准确率不得低于旧基线，人工改选率不得上升。
- token 必须扣除 router/index/adapter 开销后净节省大于 0；宿主不支持或未证明 deferred loading 时只能记为 `not_measurable`。P95 附加延迟目标不超过 1 秒。
- offline fixture、HMAC 日志存在、review PASS 或元数据晋升，任何单项都不能单独关闭观察窗。

## 本候选明确延期

本版不做全量自动重分类、在线自学习、远端遥测、自动安装、自动晋升、自动执行、宿主 hook、live/default-on 或收益结论。以后若申请 live，必须先用独立候选证明宿主 adapter 的 fail-open、权限保持、回滚和真实价值，再由用户对准确目标明确批准。
