# 依赖说明

## 运行时

- Python 3.10 或更高版本；
- 核心运行时只使用 Python 标准库；
- Unix-like 文件系统，用于文件锁和权限检查。

多 Agent 面板使用标准库 loopback HTTP 服务，只绑定项目本地入口。本仓库不会安装或启动外部群聊服务。

## 完整回归测试

完整 271 项回归测试需要 `requirements-test.txt` 中的 `jsonschema>=4,<5`。CI 固定使用 macOS 15 与 Python 3.12，因为受控 one-shot 编排依赖系统 `sandbox-exec`；缺少该隔离层时会按设计 fail closed。

## 可选集成

Agent provider、登录、API key、付费服务、网络访问、watcher、launchd、cron 与真实 host adapter 均不随本包安装或启用，必须由用户另行配置和授权。

`publish.sh` 只是一项带显式确认门的人工 GitHub 发布辅助脚本，不属于正常 Skill 执行路径。
