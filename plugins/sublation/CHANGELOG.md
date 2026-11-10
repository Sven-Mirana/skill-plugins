# Changelog

## v5.0.0 - 2026-08-25

### Added

- 可按项目限缩或扩展席位的多 Agent 协作面板，以及项目本地 loopback UI/API。
- 默认关闭、必须显式授权的受控唤醒意图、租约、声明与可验证收据契约。
- 11 个 Skill 领域、16 项能力标签与中央元数据分类目录。
- 只提供建议、不替代宿主选择的 shadow 精准路由，配套 39 个合成场景。
- 面板、唤醒、分类、路由、回退与回滚所需的 Schema、fixtures、验证索引和测试。

### Changed

- 对中国大陆使用场景优先采用简体中文界面和说明。
- 收紧项目根、房间身份、席位代次、幂等键、HMAC 与排他收据的验证边界。
- 完整回归扩大到 271 项，并加入可复现公开包、隐私和确定性索引验证。

### Retained

- V4 的 telemetry、value-delta、one-shot、回滚与最终用户授权边界。
- 席位、唤醒意图、排队或声明不等于 Agent 已真实回复或给出 PASS。
- 路由建议不等于实际调用；本版本不启用 watcher、launchd、cron、真实 host adapter 或 live routing。

## v4.0.0 - 2026-07-29

### Added

- Controlled scenario taxonomy and deterministic route-map validation.
- Explicit positive, negative, ambiguous, and handoff boundaries for Skill routing.
- Opt-in privacy-minimal usage events and session-free aggregate reports.
- Evidence-tiered value-delta delivery reports.
- Schemas and focused tests for routing, telemetry, and delivery reports.

### Changed

- Public terminology now uses generic user and review roles.
- One-shot and Loop Engineering helpers use the current hardened formal runtime.
- Release documentation states exact dependency, privacy, telemetry, and authorization boundaries.

### Retained

- V3 durable run ledger, independent role separation, evidence binding, approval receipts, rollback-aware promotion, and fail-closed automation.
- Installation, formal replacement, alias changes, deletion, telemetry activation, and publication remain separately authorized user actions.

## v3.0.0 - 2026-07-14

### Added

- Executable Loop Engineering gate and decision-packet generator.
- Durable one-shot run ledger, bounded worker orchestration, local ephemeral adapter, approval receipt verification, and rollback-aware promotion helpers.
- Run and worker configuration schemas.
- Tests for loop automation, durable runs, orchestration, receipts, adapters, and promotion recovery.

### Changed

- The skill entrypoint now documents explicit-trigger one-shot behavior and `USER_DECISION_REQUIRED` as the default automation endpoint.
- Release guidance binds evidence, review state, reports, approvals, candidate revisions, and baseline hashes before formal writes.
- Public release text uses generic user/approver roles and excludes internal incident records and private runtime artifacts.

### Retained

- V2 configurable review seats, strict audit, value-delta gate, decision history, observation windows, and evidence-not-authority model.
- Formal skill roots remain read-only until explicit user approval.

## v2.0.0 - 2026-06-13

### Added

- Configurable review policy metadata for default three-agent, configured multi-agent, single-agent, and user-waived review modes.
- Explicit value-delta, decision-history, independent-reproduction, rejected-alternative, redacted-reporting, and post-promotion safety checks.
- Strong-path checks and bytecode-aware patch verification for cleanup candidates.
- Public release package manifest and checksums.

### Changed

- Promotion evidence now treats Hermes, Claude Code, Codex, and external evaluators as evidence sources rather than promotion authorities.
- Review-seat policy is manifest-driven, while user approval remains the final boundary for formal promotion.
- Release documentation now separates public package contents from private runtime candidate and rollback records.

### Fixed

- Removed R1 unauthorized SKILL.md content instead of justifying it after the fact.
- Removed stale release-facing references and a broken template link.
- Removed bytecode residue from release candidates and formal release packaging.
- Replaced release-facing personal-path examples with portable placeholders.

## v1.0.0 - 2026-05-29

- Initial public release package for the Skill Sublation governance pipeline.
- Candidate, audit, lifecycle, and observation helpers.
- Manifest v3 and observation v3 schemas.
- Initial release checklist and audit report.
