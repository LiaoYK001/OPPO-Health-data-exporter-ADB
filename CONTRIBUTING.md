# 贡献指南

感谢改进数据可携带性、厂商适配、校验或本地展示。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
cd .\webapp
npm ci
```

提交前在仓库根目录运行：

```powershell
python -m pytest
python -m ruff check health_exporter tests scripts ohealth_exporter.py
python -m bandit -r health_exporter ohealth_exporter.py -x health_exporter/web_dist -ll
python .\scripts\public_release_check.py
python .\scripts\license_compliance_check.py
cd .\webapp
npm test
npm run build
```

修改前端时应提交同步生成的 `health_exporter/web_dist/`。新增运行时依赖时，要说明必要性、许可证和安全影响，并更新 `THIRD_PARTY_NOTICES.md`（若分发了其代码或素材）。

## 隐私要求

- 测试和文档只能使用人工生成的合成数据；简单删掉姓名并不足以去敏健康数据。
- 不提交 `data/`、数据库、截图、UI XML、日志、导出表、密钥、盐、绝对用户目录或真实设备/账号标识。
- PR 描述和测试失败输出也属于公开内容。提交前检查终端复制内容，并运行公开发布检查。
- 厂商 fixture 必须最小化，改变日期、数值、标识和关联结构，且不能从真实个人记录直接派生。

## 代码与数据契约

- 保留不可变原始层、规范层和证据之间的可追溯关系，不静默修正或伪造精细序列。
- 新厂商逻辑放入适配器，不为 OPPO、小米或华为在核心表增加专属必填字段。
- 对数据库迁移、时区、单位、revision、`partial`、无数据和多来源选择补充测试。
- 安全检查的抑制必须紧邻代码并写清固定输入、参数绑定或其他可验证理由。

提交贡献即表示你有权提交相关内容，并同意按仓库的 [MIT License](LICENSE) 许可该贡献。行为规范见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)；安全问题不要走普通 PR，按 [SECURITY.md](SECURITY.md) 私下报告。
