# Sublation V5.0 完整发行包

这是技能扬弃（Sublation/Aufhebung）V5.0 的 MIT 许可、简体中文完整发行包。
它保留正式 V5 的多 Agent 协作面板、默认关闭的受控唤醒意图层、Skill 分类与
shadow 精准建议、治理 Schema、合成 fixtures 和完整回归测试。

## 重要边界

- 面板席位不等于复核授权，排队或唤醒意图不等于 Agent 已回复。
- 路由只提供 shadow 建议，不执行 Skill，不改变宿主选择，不授予权限。
- 自动唤醒默认关闭；本包不安装、不注册也不启用 watcher、launchd、cron 或真实
  host adapter。
- 用户明确批准前，不得写正式技能根、晋升候选、发送外部消息或上传材料。
- 本包不含群聊记录、案件材料、凭据、运行态收据或本机治理事务。

## 安装

把 ZIP 解压后，将唯一顶层目录 `skill-sublation` 放入目标 Agent 的技能目录。
已存在同名目录时先独立备份并核对，不要覆盖安装。新安装通常在 Agent 的下一轮
任务或重启后生效。

## 验证

运行时以 Python 3.10 或更高版本为宜。完整回归需要 `jsonschema`：

```bash
python3 -m pip install -r requirements-test.txt
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -B -m unittest   discover -s scripts -p 'test_*.py'
```

发布构建时上述命令应显示 `Ran 271 tests` 与 `OK`。构建、测试或压缩成功不等于
已安装、已上传、已审核上架或观察窗已经关闭。
