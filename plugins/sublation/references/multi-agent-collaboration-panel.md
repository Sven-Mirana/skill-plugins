# V5.0 多 Agent 协作面板

本面板把现有四方群聊的项目隔离、回环服务、追加式消息、精确游标和非破坏性归档原则，扩展为可由用户按任务限缩或拓展的动态席位。它是协作控制面，不是权限控制面，也不改变 Sublation 的正式晋升门。

## 1. 两种席位必须分开

- **协作席位**：决定谁显示在面板、接收哪些新消息、能否承接任务以及采用何种已注册适配方式。
- **治理复核席位**：由 candidate manifest 的 `validation.review_policy` 决定，约束 builder、independent verifier、reviewer 和 coordinator 的独立性与最低报告数。

用户在面板里新增、隐藏、停用或恢复协作席位，不会自动改变 `review_policy`，不会降低独立复核强度，也不会授予文件、网络、凭据、安装、晋升或 Skill 执行权限。若要调整治理复核席位，必须另有本次 run 的用户授权并更新候选账本。

roster 中的 `adapter_id` 只描述协作投递画像，不是自动唤醒适配器，也不证明对应工具已安装、在线或获权。wakeup policy 的 `adapter_id` 属于另一套、需另行 fingerprint 登记的宿主适配器命名空间；两者不得自动互推。

## 2. 项目本地数据

默认数据根为项目内 `.sublation-panel/`：

```text
.sublation-panel/
├── config.json
├── roster.json
├── panel-events.jsonl
├── roster-events.jsonl
├── messages.jsonl
├── tasks.jsonl
├── routing-display.jsonl
├── cursors/<seat-id>.json
├── app/{index.html,app.js,styles.css}
├── wakeup/                  # 仅在用户显式初始化后出现
│   ├── binding.json
│   ├── policy.json
│   ├── cursors.json
│   ├── intents.jsonl
│   ├── events.jsonl
│   ├── completion-journal.json
│   ├── .receipt-key
│   └── .lock
└── .lock
```

服务只绑定 `127.0.0.1`。不同项目不得共享数据根、游标、主题或端口配置。`messages.jsonl`、`tasks.jsonl`、`roster-events.jsonl` 和 `routing-display.jsonl` 均只追加；面板不能通过清屏、归档、缩席或停用来截断这些文件。

## 3. 席位生命周期

默认房间保留 `user/root`、Codex、Hermes、Claude Code 四席，默认项目上限为 12 个 Agent 席；初始化时用户可显式把上限设为 1 至 32。固定用户席不计入该上限。

### 新增

新增席位必须：

1. 校验规范化 seat ID，拒绝保留 ID、路径分隔符、`..` 和重复或曾用 ID；
2. 默认使用 `queue_only` 与 `generic-queue`，不能从网页写任意命令、环境变量、凭据或 webhook secret；
3. 在同一文件锁内读取当前最后一条消息，将新游标初始化到该消息，再原子更新 roster；
4. 写入包含前后 roster hash 的 append-only 事件。

因此新席不会继承加入前的广播，也不会凭空产生历史积压。

### 隐藏与停用

- `visible=false` 只折叠面板卡片，不停止队列。
- `active=false` 停止新投递，但保留 seat tombstone、历史消息、任务和 cursor。
- 存在未确认消息或未结任务时拒绝停用；应先确认、转派或由用户明确处理积压。
- `user` 席永远不能停用，`system` 与 `all` 是保留 ID。
- seat ID 永不复用。恢复只能恢复原 tombstone，不能创建同名的新身份。

## 4. 消息、游标与任务

- 有席位、有进程或认证成功都不等于在线、已接收或已完成。
- 读取 inbox 不推进游标；只有精确 `ack-through` 才能确认到指定消息，且游标不得倒退。
- 广播只投递给消息写入当时已加入、active 且 `receives_broadcast=true` 的席位。
- task owner 必须是当时 active 且 `can_own_tasks=true` 的注册席位；历史 owner 即使已成为 tombstone 仍可读。
- 未知历史 speaker 显示为历史身份，不得伪装成系统消息。

## 5. Skill 分类、建议与调用的四层状态

面板必须分别显示：

1. `metadata_classification`：Skill 领域、能力和 `suggested/candidate/approved` 状态；其中 `approved` 只表示 routing 元数据已审阅，不表示 Skill 已安装、宿主可见、获权或已调用；
2. `advisory_recommendation`：shadow router 的 `recommend/clarify/compose_or_clarify/fallback` 建议；
3. `host_selection`：具体 Agent 宿主自己的 baseline 与 effective selection，V5 当前必须保持不变；
4. `actual_invocation`：只有本地 verifier 对 `routing-invocation-receipt-v1.json` 的 schema、项目/请求/adapter 绑定与签名全部验真后，才可显示“已调用”或“已完成”；仅有 receipt ID 或 sha256 字符串不够。

没有调用收据时，固定显示 `not_performed`。router 的 `fallback_used=true` 只是回退请求，不是宿主已经执行旧 catalog；只有宿主 fallback execution receipt 才能显示“已回退”。

当前 V5.0 集成的是：

- 自动生成的分类候选；
- 经审阅元数据上的精准建议 shadow；
- 默认关闭、拒绝原始提示词的 host-shadow 适配层；
- 面板四层状态展示。

当前明确不包括：全量正式 Skill 分类、任一宿主 hook、自动加载或调用 Skill、live/default-on 路由、凭据或权限扩张、远端遥测、自动晋升。真正的自动精准调用必须由每个宿主分别建立 active catalog 绑定、权限与依赖检查、fallback、回滚和调用收据，并经新候选和用户批准。

## 6. 路由隐私

面板和持久日志不得保存原始提示词、对话、文件正文、案件事实或凭据。只允许不透明 request ID、封闭 feature ID、分类/理由码、Skill ID、hash、状态与时间。案件事实不得进入协作群聊。

## 7. 运行

```bash
# 在目标项目建立独立控制面；已有目录时拒绝覆盖
python3 scripts/collaboration_panel.py init \
  --project <project-path> \
  --user-name root

# 查看与验证 roster
python3 scripts/collaboration_panel.py roster --project <project-path>
python3 scripts/collaboration_panel.py validate --project <project-path>

# 新席默认 queue-only
python3 scripts/collaboration_panel.py add-seat \
  --project <project-path> \
  --seat-id specialist-a \
  --display-name "专项复核 A" \
  --role-label "领域复核" \
  --expected-revision <revision> \
  --reason "本任务需要额外领域复核"

# 本地启动；端口冲突时显式换端口
python3 scripts/collaboration_panel.py serve \
  --project <project-path> \
  --host 127.0.0.1 \
  --port 18787
```

默认界面使用简体中文。席位卡动态渲染，宽屏自适应排列，小屏单列；超过八个可见 Agent 时折叠，用户可展开查看全部。

面板会只读显示每席位的唤醒意图状态和受控窗口状态。建席、隐藏、恢复或打开网页都不会初始化、arm 或启动唤醒。`scripts/agent_wakeup.py` 只管理项目账本、intent、claim 和收据校验；真实启动或调用只能由另行安装、授权并登记 fingerprint 的宿主适配器执行，本候选不包含该安装。详见 `references/agent-auto-wakeup.md`。

## 8. 迁移

旧四方房间没有 roster 文件时，只读合成 `user + codex + hermes + claude-code`，不得在服务启动时静默落盘。显式迁移先 `--dry-run`，用户确认后再 `--apply`；旧消息、任务、游标和归档保持原样。当前 8787 自定义四方项目不是标准 `.four-party-chat` 模板，迁移前必须单独备份 runtime、复验项目身份和 cutoff，不能直接覆盖其 UI 或数据。
