# Skill 插件仓库

本仓库公开六个 Skill 与插件项目。源码与安装包可从 Releases 下载，适用许可见各包内的 LICENSE 和来源声明。

[六项下载](<https://github.com/Sven-Mirana/skill-plugins/releases/tag/skill-bundle-2026.11.10>) · [版本清单](PACKAGES.json) · [SHA-256 校验值](SHA256SUMS.txt)

| 项目 | 版本与下载 | 形态 |
| --- | --- | --- |
| Sublation | [5.0.0-claude-code.4](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/sublation-5.0.0-claude-code.4-9f340ce-github-source.zip) | Claude Code 插件源码 |
| 皋陶 | [1.0.0-rc.16](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/gaotao-native-github-full-1.0.0-rc.16.zip) | 插件完整源码包 |
| 买卖合同纠纷 | [1.0.0-rc.13](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/sales-contract-native-github-full-1.0.0-rc.13.zip) | 插件完整源码包 |
| 追偿权纠纷 | [3.1.3-wb-r7](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/dsh-recovery-right-wbr7-3.1.3-wb-r7.tgz) | DeepSeek Harness 原生安装包 |
| 融资租赁普通直租纠纷 | [1.0.0-rc.10/b21](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/financial-lease-direct-native-1.0.0-rc.10-b21.zip) | 普通直租插件完整源码包 |
| 诉讼可视化 | [1.0.0-rc.2](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/litigation-visualization-native-1.0.0-rc.2-github-source.zip) | 原生插件源码树 |

## Sublation 安装

```bash
claude plugin marketplace add Sven-Mirana/skill-plugins
claude plugin install sublation@sven-mirana-skill-plugins
```

在 Claude Code 会话输入 `/sublation:sublation`。追偿权使用 DeepSeek Harness 原生安装包，其余项目的使用方式见各包说明。

融资租赁公开范围为普通直租纠纷。

## 许可证

本仓库自行编写的发布目录、仓库级清单和说明文档采用 [AGPL-3.0-only](LICENSE)。

`plugins/` 中的源码及 Releases 下载包各自适用随附的许可证和来源声明。六个项目的许可见 [版本清单](PACKAGES.json)，第三方依赖按各自随包许可分发。
