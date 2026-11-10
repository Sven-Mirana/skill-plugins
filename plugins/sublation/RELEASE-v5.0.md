# Skill Sublation v5.0.0

## 概览

V5.0 在既有可审计扬弃流程上新增可伸缩的多 Agent 协作面板，以及 Skill 分类和精准调用建议层。面板允许用户按项目限缩或扩展席位；路由器根据任务元数据给出 shadow 建议，同时完整保留宿主的原选择。

## 本次新增

- 简体中文多 Agent 协作面板，支持席位增减、角色、任务与时间线；
- 默认关闭、必须显式武装的受控唤醒意图、租约、声明与收据契约；
- 11 个领域、16 项能力的 Skill 分类体系；
- 中央 advisory-only shadow router、39 个合成路由场景与确定性索引；
- host-shadow 元数据、回退/回滚契约及配套严格 Schema；
- 271 项完整回归测试和可复现公开包验证。

## 安全边界

- 席位、排队、唤醒意图或声明不证明 Agent 已回复，也不构成 PASS。
- shadow 推荐不执行 Skill、不改变宿主选择、不授予权限。
- 自动唤醒默认关闭；本版本不安装或启用 watcher、launchd、cron、真实 host adapter 或 live routing。
- 不含群聊记录、案件材料、凭据、候选与治理运行态。
- 安装、外部消息、正式根写入、观察窗关闭和平台上架仍分别需要明确授权。

## 来源与包绑定

- 正式源：158 文件，逻辑树 `sha256:8276768668a9f88c182e0046fb9310b4a90b700a87bb21ef04136a0ece9dc9cb`；
- 计划随 `v5.0.0` 发布的 asset：`skill-sublation-v5.0.0-full-mit-20260825.zip`；
- ZIP：170 文件，498,689 bytes；
- ZIP SHA-256：`1a13d4ff07526873564c9fa21b15ecdfa87eae8e6a240951bc10367e88f8110a`；
- fresh-extract tree：`sha256:d2eb71dc89ad8e53f7c04ae38194114bde61ab56ef3e30e5769e6095c5f26e36`；
- `SHA256SUMS.txt`：169 项全部通过；
- 两次独立构建逐字节一致；隐私、凭据、案件与真实群聊材料命中均为 0。

与本标签配套的 GitHub Release 仅计划附完整 MIT ZIP；远端 tag、Release 与 asset 的实际状态以 API 读回后另存的 `REMOTE-PUBLICATION-RECEIPT.json` 为准。SkillHub 中国大陆简化包不在本发布计划内，由用户自行上传。

## 验证

运行时建议 Python 3.10 以上；完整回归需要 `jsonschema>=4,<5`：

```bash
python3 -m pip install -r requirements-test.txt
shasum -a 256 --check SHA256SUMS.txt
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -B -m unittest \
  discover -s scripts -p 'test_*.py'
```

预期输出包含 `Ran 271 tests` 与 `OK`。测试通过不代表已经安装、启用宿主集成、关闭观察窗或完成任何平台审核。

## 从 V4 升级

不要覆盖现有安装。先独立备份旧目录，解压 V5，核对校验和并完成本地测试后，再按各 Agent 的安装规则切换。V4 的 telemetry、value-delta、one-shot 与回滚边界继续保留。
