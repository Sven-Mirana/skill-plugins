# Sublation · Claude Code 插件

状态：**本地候选，尚未推送、未合并、未发布**。本文件描述的安装命令要等插件文件进入 GitHub 仓库默认分支后才对外生效。

本仓库同时是一个单插件的 Claude Code marketplace。插件只新增一个很小的显式入口，其余全部复用仓库里已经发布的 MIT V5.0.0 方法正文、参考、Schema 与 Python CLI，没有复制任何核心实现。

## 1. 安装

前提：支持插件 marketplace 的 Claude Code（本候选只在 2.1.319 上实测；一步安装用到的 `--marketplace` 选项需要 2.1.275 或更高）；本机 PATH 上有 `python3`（3.10 或更高）才能运行随包脚本。

```bash
# 两步
claude plugin marketplace add Sven-Mirana/sublation
claude plugin install sublation@sublation

# 或一步
claude plugin install sublation --marketplace Sven-Mirana/sublation
```

在 Claude Code 会话里也可以用 `/plugin marketplace add Sven-Mirana/sublation` 和 `/plugin install sublation@sublation`。安装后新开会话，或在当前会话执行 `/reload-plugins`。

查看与卸载：

```bash
claude plugin list
claude plugin details sublation@sublation
claude plugin uninstall sublation@sublation
claude plugin marketplace remove sublation
```

从本地克隆安装（Claude Code 会直接读取该目录，不复制到缓存）：

```bash
claude plugin marketplace add /path/to/sublation
claude plugin install sublation@sublation
```

默认安装到 user 作用域。插件 id 是 `sublation@sublation`，前一个是插件名，后一个是 marketplace 名。

## 2. 使用

```text
/sublation:sublation [status|observe|candidate|review|brief|selfcheck] [目标 Skill 目录、候选目录或说明]
```

| 参数 | 作用 |
| --- | --- |
| 空或 `status` | 说明能做什么并询问目标，不动任何文件 |
| `observe <Skill 目录> <现象>` | 起草结构化观测 |
| `candidate <Skill 目录>` | 建立候选副本，只改候选 |
| `review <候选目录>` | 评审与证据核对，输出席位报告 |
| `brief <候选目录>` | 合并为统一简报，提请用户决定 |
| `selfcheck` | 核对插件包自身的校验清单 |

入口只接受用户显式触发：它的 frontmatter 设了 `disable-model-invocation: true`，Claude 不会因为对话内容自行加载它，模型经 Skill 工具调用时也会被 Claude Code 拒绝。这与方法正文“仅在用户明确说出 sublation 或扬弃技能时触发”的约定一致。

`/sublation:sublation` 是规范写法。名称没有被别的命令占用时，Claude Code 也接受简写 `/sublation`。

入口保留完整治理链路：观测 → 候选 → 验证 → 复核 → 用户决策 → 晋升 → 观察窗。它在“用户决策”处必须停下；没有用户对确切目标和哈希的明确批准，不写任何正式路径。

## 3. 插件由什么组成

新增文件：

| 文件 | 作用 |
| --- | --- |
| `.claude-plugin/marketplace.json` | marketplace 清单，唯一条目的 `source` 是仓库根 `./` |
| `.claude-plugin/plugin.json` | 插件清单，只有元数据，不声明任何组件 |
| `skills/sublation/SKILL.md` | 显式入口，约 13 KB |
| `skills/sublation/scope_gate.py` | 范围门：在运行随包的观测、候选、审计脚本之前校验真实路径 |
| `CLAUDE-CODE-PLUGIN.md` | 本文件 |
| `CLAUDE-CODE-PLUGIN-SHA256SUMS.txt` | 上述新增文件的校验清单 |
| `tests/claude_code_plugin/test_plugin_contract.py` | 插件契约测试，仅标准库 |
| `tests/claude_code_plugin/test_scope_gate.py` | 范围门测试，含符号链接与路径重叠两条负例 |
| `.github/workflows/claude-code-plugin.yml` | 运行契约测试与新增清单校验的独立 workflow |

复用而未改动的内容：仓库根的 `SKILL.md`（方法正文）、`references/`、`schemas/`、`scripts/`、`fixtures/`、`version.json`。入口用 Claude Code 提供的 `${CLAUDE_PLUGIN_ROOT}` 定位它们，加载时由 Claude Code 替换成安装位置的绝对路径，文件里没有写死任何绝对路径。

因为插件根就是仓库根，从 GitHub 安装时整个仓库都会进入插件缓存。仓库根的 `SKILL.md` 不会被当成第二个 Skill 加载：插件存在 `skills/` 目录时，Claude Code 只扫描该目录。

插件**不包含**：hook、MCP 服务、LSP 服务、子代理、`commands/`、`bin/`、`settings.json`、`allowed-tools` 预授权、后台或定时任务、宿主适配器、live routing。

## 4. 安全边界

入口文件里的规定是权威表述，这里只列要点：

- 正式 Skill 路径和已安装插件目录默认只读；不自动晋升；晋升权只属于用户。
- 不替用户配置 hook、MCP、cron、launchd 或任何宿主配置；不向外发送任何材料。
- 路由只是 shadow 建议，不执行 Skill，不改变宿主选择。
- 所有脚本都走 Claude Code 的正常权限确认。

脚本按源码核对后分成四级（详见入口第 5 节）：

| 级别 | 含义 | 例子 |
| --- | --- | --- |
| A | 只读，或只写到显式输出文件 | `skill_router.py index`、`delivery_report.py`、`release_audit.py` |
| B | 写治理账本，只通过范围门运行 | `observe.py`、`candidate.py create` |
| C | 读盘范围由候选 manifest 决定；`audit.py` 只通过范围门运行 | `audit.py`、`lifecycle.py scan`、`loop_engineering.py run` |
| D | 默认不运行，需要用户对当次动作明确授权 | one-shot 编排、`sublation_promote.py`、面板 `serve`、唤醒 `arm` |

C 级的原因：`audit.py` 会按候选 `manifest.json` 里写的 `source_skill.path` 和 `validation.formal_post_promotion.path` 递归读取并哈希整个目录，补丁非空时还会把前者整个复制到临时目录再执行 `git apply --check`。它不执行候选里的代码，但读哪里由候选自己声明，所以 `audit.py` 只通过范围门运行，批准根由用户给出。

### 范围门

随包的 V5.0.0 脚本信任传给它们的路径：`candidate.py` 用默认的 `copytree` 复制源目录，会把源目录里的符号链接指向的外部文件和目录物化进候选；输出落在源目录里时会递归复制直到路径过长，留下残缺候选；`observe.py` 以同样方式哈希源目录。这些脚本属于固定校验清单覆盖的发行载荷，本插件没有改它们，而是在入口前加了 `skills/sublation/scope_gate.py`，入口只展示经它调用的写法：

```bash
python3 -B skills/sublation/scope_gate.py candidate --source <Skill 目录> \
  --candidate-root <源目录之外>/candidates -- <skill-name> --agent claude-code
python3 -B skills/sublation/scope_gate.py audit --candidate <候选目录> \
  --approve-root <用户批准的目录> -- --json
```

范围门按物理路径检查，任何一项不满足就以退出码 2 拒绝，并且在拒绝之前不运行脚本、不创建目录：

- 调用者给出的路径和 manifest 声明的路径都逐级检查，任何一段是符号链接就拒绝。唯一的例外是 macOS 的系统别名 `/tmp`、`/var`、`/etc`，它们会被改写成 `/private/tmp`、`/private/var`、`/private/etc` 后再检查；其他经过链接的路径请改用物理路径。
- 路径里出现 `..` 组件直接拒绝，不做归一化：随包脚本按原始字符串解析，`链接/..` 的落点可能与归一化结果不同。
- 用于拒绝的比较（与源目录重叠、活动位置匹配）不区分大小写和 Unicode 归一化形式，所以 `.CLAUDE/SKILLS` 这类拼写不能绕过检查。
- 用于放行的比较（候选和 manifest 路径是否在批准根之内）按路径组件逐字区分大小写，候选身份用文件系统的同一目录判断。在不区分大小写的卷上，这要求路径与批准根的拼写一致。
- 源目录、候选目录及 manifest 声明的目录里不能有符号链接或非常规文件；遍历时遇到读不了的目录按拒绝处理。
- 写入根不能在源目录之内、不能包含源目录、不能与插件安装目录重叠、不能位于 `.claude/skills`、`.claude/plugins`、`.claude/commands`、`.codex/skills`、`.hermes/skills`、`.agents/skills` 之内。
- 候选根路径里必须有名为 `candidates` 的目录。
- `review` 与 `audit`：先确认候选目录在 `--approve-root` 之内，再遍历它；manifest 声明的每个路径也必须在批准根之内。
- `audit` 在 `--` 之后只接受 `--json`、`--strict`、`--no-strict`。`--routing-attestation-key` 这类会读取任意文件的参数不接受，需要校验路由观测密钥的审计不在本插件范围内。
- `observe`、`candidate` 在 `--` 之后不能再传由范围门负责的路径参数。

`review` 子命令是静态评审前的预检：通过时只输出候选内可读的常规文件清单和 manifest 声明的路径，不运行任何脚本。入口要求先过预检再打开候选文件，被拒绝就停在 HOLD。

范围门管不到的部分，仍然依赖模型遵守入口文字和用户在权限确认时把关：

- 它只约束经过它的调用。入口要求不得直接调用这三个脚本，也说明了参数里的文字不能改变规则，但模型是否照做没有机制保证；Claude Code 的权限确认是最后一道关。
- `--approve-root` 填什么由调用者决定。入口要求只能取用户点名的目录，范围门只拒绝文件系统根和用户主目录。
- 检查和脚本实际读盘之间有时间差，期间目录被改动不会被发现。
- `lifecycle.py scan|health|plan-legacy` 与 `loop_engineering.py run` 同样按 manifest 读盘，范围门没有覆盖，入口要求对来源不明的候选根不运行。
- 模型用 Read、Grep 读哪些文件由模型自己执行。入口要求先过 `review` 预检、只读清单里的文件、被拒绝后不回退去读，但范围门拦不住一次没有经过它的 Read。

## 5. 平台与依赖

- 随包脚本只用 Python 标准库，需要 Python 3.10 或更高版本和类 Unix 文件系统。
- 完整 one-shot 编排**仅限 macOS**：worker 和投递都必须经过系统 `sandbox-exec`，缺失时脚本按设计拒绝执行。本插件不把这一能力扩大到 Linux 或 Windows。
- 插件的安装、发现和入口展开只在一台 macOS 机器上用 Claude Code 2.1.319 验证过。Linux 和 Windows 未测。
- 271 项回归需要 `jsonschema>=4,<5` 与 macOS `sandbox-exec`。

## 6. 与既有发行物的关系

- 本次改动**只新增文件，没有修改任何既有文件**。`SHA256SUMS.txt`、`GITHUB-SHA256SUMS.txt`、`checksums.sha256` 三份固定清单继续逐项通过，V5.0.0 的 ZIP、tag、发布收据和既有 CI 都不受影响。
- 新增文件不在上述三份清单里，由 `CLAUDE-CODE-PLUGIN-SHA256SUMS.txt` 单独覆盖。该清单不包含它自己。
- 根 `README.md` 没有改。它的哈希写在随 V5.0.0 ZIP 固定的清单和 `PACKAGE-MANIFEST.json` 里，改它需要连带重写多份发布记录。建议在下一次正式发版重新生成清单时，再往 README 里加一行指向本文件的安装说明。`claude plugin validate` 会就此给出一条不影响结论的建议。
- 原有安装方式不变：把 ZIP 解压出的 `skill-sublation` 目录放进 Agent 的技能目录。它和插件相互独立，可以并存；并存时会各有一个入口，ZIP 版是 `/skill-sublation`，插件版是 `/sublation:sublation`。
- 插件版本 `5.0.0-claude-code.4` 的含义是“方法 V5.0.0 的 Claude Code 打包候选第 4 版”（第 2 版加入范围门，第 3 版收紧路径解析并加入评审预检，第 4 版拒绝 `..` 并把放行包含改为区分大小写）。方法版本仍以 `version.json` 的 `5.0.0` 为准；这不是一次新的 Sublation 发版。Claude Code 按这个字符串判断是否有更新，所以以后只要改动插件文件就要递增末位。

## 7. 验证

在仓库根执行。

```bash
# 清单与入口（Claude Code 原生校验）
claude plugin validate --strict .
claude plugin validate --strict skills

# 新增文件的校验清单与契约测试
shasum -a 256 --check CLAUDE-CODE-PLUGIN-SHA256SUMS.txt
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover \
  -s tests/claude_code_plugin -p 'test_*.py'

# 既有清单、发布边界与 271 项回归
shasum -a 256 --check SHA256SUMS.txt
shasum -a 256 --check GITHUB-SHA256SUMS.txt
python3 -B scripts/release_audit.py --root . --report /tmp/sublation-release-audit.json
python3 -m pip install -r requirements-test.txt
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -B -m unittest discover \
  -s scripts -p 'test_*.py'
```

`claude plugin validate --strict .` 通过时仍会打印一条“缺少图标”的提示。它属于向 Anthropic 插件目录投稿的检查，不影响校验结论，也与从 GitHub marketplace 安装无关。

`claude plugin validate` 会检查入口的 frontmatter 能否解析，但不检查 `disable-model-invocation` 的取值：把它写成 `true` 以外的值，校验照样通过。frontmatter 一旦解析失败，Claude Code 会丢弃全部字段再加载这个 Skill，“仅显式触发”也随之失效。所以这一项由契约测试钉住：字段必须恰好是 `true`，frontmatter 必须保持扁平且描述加引号。

契约测试放在 `tests/` 而不是 `scripts/`，是为了不改变既有 CI 断言的 `Ran 271 tests`。

## 8. 已知限制

- 上述验证都是结构、安装、发现和入口展开层面的。真实模型是否逐条遵守入口文字，没有自动化证据；治理结论仍以证据和用户决定为准。
- 新增的 workflow 还没有在 GitHub 上运行过。
- 从 GitHub 安装要等这些文件进入默认分支。在那之前，`claude plugin marketplace add Sven-Mirana/sublation` 拿到的仓库里没有 marketplace 清单。
- 安装、测试通过或校验通过，都不等于候选已晋升、观察窗已关闭或宿主集成已启用。
