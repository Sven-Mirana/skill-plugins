# V5.0 受控 Agent 唤醒意图与收据

本协议把四方群聊中已经验证过的 watcher 与定时轮询经验，抽象为可用于任意动态席位的项目本地唤醒意图控制面。它不把现役 Claude watcher 或 Hermes cron 直接复制进 Sublation，也不安装 host adapter、launchd、cron 或其他常驻服务；默认保持关闭。

## 1. 三层必须分开

1. **发现层**：零 token 扫描项目消息，做首见基线、self-filter、受众/标签匹配，只生成耐久 wake intent。
2. **闸门层**：校验 `project_id + project_root + loopback room identity + seat_id`，执行逐席位锁、幂等、冷却、小时配额、重试退避和窗口状态判断。
3. **宿主适配层**：由另行安装、预注册且 fingerprint 固定的外部适配器真正启动 Claude Code、Hermes、Codex 或其他 Agent，并返回最小化收据。本候选不包含该安装。

发现 intent 不等于 Agent 已醒；claim 不等于模型已完成；watcher 收执不等于回复已送达。只有宿主返回符合 schema 且可验证的 `sent_and_verified` 收据，控制面才推进该席位 cursor。

## 2. 项目本地状态

显式初始化后，数据位于项目自己的 `.sublation-panel/wakeup/`：

```text
wakeup/
├── binding.json
├── policy.json
├── cursors.json
├── intents.jsonl
├── events.jsonl
├── completion-journal.json
├── .receipt-key
└── .lock
```

所有文件权限为 `0600`，目录为 `0700`。事件和 intent 只追加并带 hash chain；`completion-journal.json` 用于在回帖、事件和两份游标之间做幂等崩溃恢复。日志不保存原始提示词、案件事实、凭据或完整对话，只保存不透明 message/intent/receipt ID、状态码、hash 和时间。

`.receipt-key` 是项目级 HMAC 协调密钥，只能由受信宿主协调器读取，用于绑定 policy、claim proof 和完成收据。Agent 子进程与普通网页 API 都不得读取该文件；同一 macOS 用户本身不是进程隔离，真实宿主接入前必须另做 OS 沙箱、逐适配器权限和密钥轮换。初始化 staging 在极端 `SIGKILL/KeyboardInterrupt` 时可能遗留一个权限为 `0700/0600` 的 `.wakeup-staging-*` 目录；重试会成功，但正式启用宿主前仍应受控清理废弃 staging 并轮换密钥。

## 3. 默认关闭与固定适配器

- 面板新增席位只新增队列，不创建 wake policy；未显式列入 policy 的席位固定 `disabled/queue_only`。
- policy 只能引用 `adapter_id` 与 `adapter_sha256`。网页、消息或 policy 不得传入 argv、shell、环境变量、token、cookie、webhook secret 或任意可执行路径。
- adapter 的实际工具白名单、文件范围、网络策略、会话预算和超时由宿主进程级配置实现，不能只写在 prompt 中。
- V5 候选只提供 default-off 的 policy、intent、claim、receipt 合约与 dry-run，不直接 `Popen` Claude/Hermes，也不访问外部网络。默认 policy 不登记真实宿主适配器；无另行安装、登记和用户授权时，不得 claim，也不得生成真实完成收据。

## 4. 逐席位状态机

席位 policy、窗口和 intent 是三条正交状态，不能合并成“Agent 已在线”：

```text
席位 policy：disabled/queue_only ──另行登记适配器并授权──> enabled/intent_only

窗口：closed ──user arm──> active ──deadline/quiet/max_wakes──> draining
                                                       └─无 intent/lease──> closed
                                                       └─账本异常或未排空──> hold

intent：无 ──发现──> pending ──claim──> lease_active ──sent_and_verified──> 完成并推进 cursor
                              └─失败/超时──> backoff ──尝试耗尽──> terminal_hold
```

`active` 或“已武装”只表示意图窗口开放，不表示宿主在线、进程已启动或 Agent 已回复。

关键规则：

- 首次扫描只把 cursor 放到当前末尾，不回放历史；
- 自己发的消息与带本管线完成标签的消息不得触发自己；
- 幂等键绑定项目、席位、来源消息和 pipeline，重复扫描只返回原 intent；
- 同一席位最多一个 active lease；claim 必须匹配注册 adapter 与 fingerprint；
- 失败不推进 cursor，并按受限退避重试；超过次数进入 `terminal_hold`，只生成一次转 root 状态；
- `root_hold`、`terminal_hold` 或双消费者冲突不会自动重试；用户只能用精确绑定 intent、source、window 与 reason 的 `retry_once` 裁决重新放行一次，且该裁决不能冒充 Agent 成功；
- 确定性无关消息由发现层静默跳过，不产生宿主 claim；一旦形成 intent，只有 `sent_and_verified` 才能推进 cursor；
- 普通 ACK 与自动唤醒租约互斥；成功完成必须让 wake cursor 与面板 cursor 同步前进，故障后由 completion journal 幂等恢复，不能回退或越过来源消息；
- 隐藏卡片不停止队列；停用席位如有 active lease 或 pending intent 必须拒绝；恢复席位仍回到 queue-only，不自动重启唤醒。

## 5. 受控关窗

wake window 默认 `closed`。用户显式 arm 时只设置 deadline、最大安静轮次和最大唤醒次数；冷却、小时配额、超时与单席一租约来自已 hash 绑定的 policy，不能由 arm 临时覆盖。任何关闭条件达到后先进入 `draining`，不再创建新 intent；只有 `pending_intents=0` 且 `active_leases=0` 时才能生成 hash-bound close receipt。未清空时状态必须是 HOLD，不能为了“看起来干净”直接关窗或丢弃积压。

## 6. CLI 示例

默认 fixture 只演示账本与有界窗口：`registered_adapters` 为空，席位 policy 关闭。下面的 claim/complete 仅在用户另行批准并由宿主完成真实适配器安装、fingerprint 登记后可用；运行这些 CLI 本身不会启动 Agent。

```bash
# 只创建默认关闭的项目绑定和政策投影
python3 scripts/agent_wakeup.py --project-root <project-path> init \
  --policy fixtures/collaboration-panel/wakeup-policies.json

# 用户显式打开一个有界测试窗口
python3 scripts/agent_wakeup.py --project-root <project-path> arm \
  --deadline <RFC3339-time> \
  --max-wakes 2 \
  --quiet-cycles 2

# 零 token 发现；以下 claim/complete 需要另行登记的真实宿主适配器
python3 scripts/agent_wakeup.py --project-root <project-path> discover
python3 scripts/agent_wakeup.py --project-root <project-path> claim \
  --intent-id <intent-id> --adapter-id <registered-id> \
  --adapter-sha256 sha256:<64-hex> \
  --claim-proof-hmac hmac-sha256:<64-hex>
python3 scripts/agent_wakeup.py --project-root <project-path> complete \
  --receipt <receipt.json>

# 仅由用户对精确 HOLD 作一次性重试裁决，不表示 Agent 已成功
python3 scripts/agent_wakeup.py --project-root <project-path> dispose-hold \
  --intent-id <intent-id> --source-message-id <message-id> \
  --window-id <window-id> --action retry_once --reason <reason>

# drain 后关闭并校验全链
python3 scripts/agent_wakeup.py --project-root <project-path> close
python3 scripts/agent_wakeup.py --project-root <project-path> validate
```

## 7. Claude 与 Hermes 历史参考画像

- Claude watcher 的可取经验：零 token 轮询、首见基线、自消息过滤、逐房间锁、冷却/配额、脱离父进程组、超时转 root、自动消息来源标签。
- Hermes 轮询的可取经验：项目 workdir 固定、只在成功 POST 后推进游标、已处理目标去重、无任务静默、Agent PASS 不等于用户授权。
- 两者都不能直接当 V5 信任根。V5 必须用进程级最小权限、固定 adapter fingerprint、确定性 intent/lease/receipt 和受控关窗替代 prompt-only 纪律。

## 8. 迁移与后续真实入口验证

V5 候选阶段不修改现役 watcher、launchd、gateway 或 cron。本节是未来宿主集成的另行验收，不是本候选已完成的能力或证据。切换前必须按 `project_id + project_root + room identity + seat_id` 做排他检查，证明不会出现双消费者。

严格只读验证止于 adapter handoff 与 claim 入口的只读可达性、身份和绑定检查，不执行 claim、不 POST、不推进 cursor。若要验证真实回写，只能在合成、非敏感、隔离项目中经单独授权完成：恰好生成一个 intent、实际唤醒一次、回写一次、验证一次、cursor 推进一步，并在队列清空后立即关窗。失败则保留 HOLD 和可复算证据，不冒充完成。
