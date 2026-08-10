# 公开发布门禁

公开仓库发布不是简单把 `data/` 加入 `.gitignore`。默认门禁必须证明当前树、待提交文件和发行物不含健康数据、凭据、真实用户目录或不必要的身份信息。完整 Git 历史扫描保留为可选的加强审计；如果选择不重写历史，必须像 [AUDIT_REPORT.md](AUDIT_REPORT.md) 一样明确记录并接受残余风险。

## 每次发布

```powershell
python -m pytest
python -m ruff check health_exporter tests scripts ohealth_exporter.py
python -m bandit -q -r health_exporter scripts ohealth_exporter.py -l
python -m pip_audit . --progress-spinner off
python .\scripts\public_release_check.py
python .\scripts\license_compliance_check.py
python -m build
python .\scripts\package_contents_check.py .\dist
cd .\webapp
npm ci
npm test
npm audit --audit-level=high
npm run build
git diff --exit-code -- ..\health_exporter\web_dist
```

再人工核对：

- `git status --ignored --short` 中的本地数据确实未被跟踪；
- README、图片、测试输出和发行压缩包中没有真实健康数据或绝对用户目录；
- `LICENSE`、`THIRD_PARTY_NOTICES.md`、`PRIVACY.md` 和 `SECURITY.md` 随发行物提供；
- Web 默认仍只监听本机，局域网模式仍需显式启用和登录；
- 新依赖许可证允许当前分发方式，且依赖审计没有未处理的高风险问题。

## GitHub 仓库设置

当前树和发行物门禁通过后、切换为 Public 前，应在仓库设置中：

- 启用 Private vulnerability reporting、Dependabot alerts、secret scanning 和 push protection（账户方案支持时）；
- 为 `main` 设置分支保护或规则集，要求 CI 和 CodeQL Default Setup 通过后才能合并；
- 保持 Actions 默认 `GITHUB_TOKEN` 权限为只读；若以后从 CodeQL Default Setup 改用 Advanced Setup，应先在仓库设置中关闭 Default Setup，再单独审核 workflow 所需权限；
- 禁止 Actions 创建或批准 PR，除非以后有经过单独威胁审查的明确需求；
- 检查仓库 About、社交预览图和 Release 附件，不上传本地数据库、导出表或调试压缩包；
- 验证 `SECURITY.md` 的私下报告链接实际可用。

## 可选：Git 历史审计与去敏

`python scripts/public_release_check.py --history` 会检查敏感路径、用户主目录痕迹、提交消息、未审核的作者/提交者显示名和非 GitHub noreply 的作者/提交者邮箱。它是比默认当前树门禁更严格的历史盘点；如果不整改其发现，公开前应在审计报告中逐项披露残余风险。

历史去敏会改变提交 SHA，影响所有克隆、分支和标签。执行前应制作私有镜像备份并通知协作者；可选择：

1. 使用 `git filter-repo` 在私有副本中重写敏感内容及作者名称/邮箱，然后再次运行全部门禁并强制替换尚未公开的远端；或
2. 从审核通过的当前树创建没有旧历史的全新公开仓库，同时把原仓库保持私有。

不要在原仓库已经公开且可能被克隆后声称历史已“删除”；旧对象和第三方缓存仍可能存在。若曾提交真实凭据或健康数据，应按泄露事件处理，而不仅是清理 Git。
