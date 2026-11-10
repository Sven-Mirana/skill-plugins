# Skill 插件仓库

此私有仓库收录六个项目的指定版本。下载 ZIP 后，按包内说明使用。各项目分别适用包内的 LICENSE 和来源声明。

[六个 ZIP 下载页](https://github.com/Sven-Mirana/skill-plugins/releases/tag/skill-bundle-2026.11.10) · [文件校验值](SHA256SUMS.txt)

| 项目 | 版本 | 收录形态 |
| --- | --- | --- |
| Sublation | [5.0.0-claude-code.4](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/sublation-5.0.0-claude-code.4-9f340ce-github-source.zip) | 核查 ZIP 原件 |
| 皋陶 | [1.0.0-rc.16](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/gaotao-native-github-full-1.0.0-rc.16.zip) | 核查 ZIP 原件 |
| 买卖合同纠纷 | [1.0.0-rc.13](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/sales-contract-native-github-full-1.0.0-rc.13.zip) | 核查 ZIP 原件 |
| 追偿权纠纷 | [portable-r5](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/recovery-right-portable-r5-private-review.zip) | 运行包审阅副本 |
| 融资租赁普通直租纠纷 | [1.0.0-rc.10/b21](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/financial-lease-direct-native-1.0.0-rc.10-b21-private-review.zip) | 完整包审阅副本 |
| 诉讼可视化 | [1.0.0-rc.2](https://github.com/Sven-Mirana/skill-plugins/releases/download/skill-bundle-2026.11.10/litigation-visualization-native-1.0.0-rc.2-tree-private-review.zip) | 55 个文件的原生源码树 |

## Sublation 安装

使用已获得此私有仓库 Git 访问权限的账号执行以下命令。

```bash
claude plugin marketplace add Sven-Mirana/skill-plugins
claude plugin install sublation@sven-mirana-skill-plugins
```

在 Claude Code 会话输入 `/sublation:sublation`。其余五项的适用宿主见包内说明。

## 版本说明

追偿权使用 portable-r5 的运行包。普通直租使用 rc.10/b21。诉讼可视化使用原生 rc.2 源码树。
三份审阅副本的具体范围和原始 SHA-256 记录在清单内。原许可与来源声明随文件保留。

[版本和来源清单](PACKAGES.json)
