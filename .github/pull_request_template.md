## 变更

说明问题、实现和数据契约/兼容性影响。

## 验证

- [ ] `python -m pytest`
- [ ] `python -m ruff check health_exporter tests scripts ohealth_exporter.py`
- [ ] `python -m bandit -r health_exporter ohealth_exporter.py -x health_exporter/web_dist -ll`
- [ ] `python .\scripts\public_release_check.py`
- [ ] `python .\scripts\license_compliance_check.py`
- [ ] 前端变更已运行 `npm test`、`npm run build` 并提交同步产物

## 隐私、安全与许可

- [ ] 仅使用合成 fixture；PR、日志和截图不含真实健康数据、身份信息、凭据或绝对用户目录
- [ ] 新依赖已说明必要性、安全影响和许可证，并按需更新第三方 notices
- [ ] 没有降低本机监听、只读数据库、路径校验、来源追溯或保真规则
- [ ] 这不是应按 `SECURITY.md` 私下报告的未修复漏洞
