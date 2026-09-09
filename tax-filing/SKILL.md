---
name: tax-filing
description: 公司报税技能（中国·一般纳税人）。当用户提到报税、申报、纳税、增值税、企业所得税、印花税、附加税、发票台账、进项/销项、税额计算、汇算清缴、电子税务局、完税等时使用。流程：先提示勾选认证进项 → 更新发票台账（tools/ledger.py）→ 计算申报数据（tools/calc_tax.py）→ 按电子税务局流程申报（references/e-tax-bureau.md）→ 留档；各税种税率与政策依据见 references/taxes.md。公司档案：科技有限公司（一般纳税人，现代服务 6%，小型微利企业）。
---

# 公司报税（中国 · 一般纳税人）

## 公司档案（修改公司信息或税率时更新 tools/taxconfig.py）
- 主体：科技有限公司（一人有限责任公司，一般纳税人）
- 主营：现代服务（增值税销项税率 6%）
- 税收身份：小型微利企业（默认享受企业所得税实际税负 5% 与附加税费减半；若当年不满足「335」标准，用 calc_tax.py 的 --not-small-profit 调整）

## 申报日历（截止日遇法定节假日顺延，以电子税务局公告为准）
| 税种 | 频率 | 截止 |
| --- | --- | --- |
| 增值税 + 附加税费 | 按月（主管税务机关核定按季的除外） | 次月 15 日前 |
| 个人所得税（工资薪金代扣代缴） | 按月 | 次月 15 日前 |
| 企业所得税（预缴）+ 财务报表 | 按季 | 季度终了后 15 日内 |
| 印花税 | 按季（或按次） | 季度终了后 15 日内 |
| 企业所得税汇算清缴 | 年度 | 次年 5 月 31 日前 |
| 工商年报 | 年度 | 次年 6 月 30 日前 |
| 残疾人就业保障金 | 年度 | 以当地税务局通知为准 |

## 每月申报流程（用户说「这个月报税」「帮我算税」时执行）
1. **先提醒勾选认证进项**：电子税务局 → 税务数字账户 → 抵扣勾选当月进项发票并统计确认（操作见 references/e-tax-bureau.md；未认证的进项当期不能抵扣）
2. **更新台账**（数据存 data/ledger.json）：
   - 销项：`python "${CLAUDE_SKILL_DIR}/tools/ledger.py" add-sale --date 2026-09-01 --customer "客户名" --amount 10000`
   - 进项：`python "${CLAUDE_SKILL_DIR}/tools/ledger.py" add-purchase --date 2026-09-02 --vendor "供应商" --total 5300 --rate 6`（未认证加 `--not-certified`，不可抵扣加 `--not-deductible`）
   - 费用：`python "${CLAUDE_SKILL_DIR}/tools/ledger.py" add-expense --date 2026-09-10 --item 工资 --amount 8000 --category salary`
3. **核对台账汇总**：`python "${CLAUDE_SKILL_DIR}/tools/ledger.py" summary --month 2026-09`
4. **计算申报数据**：`python "${CLAUDE_SKILL_DIR}/tools/calc_tax.py" --month 2026-09` → 输出增值税/附加税费/印花税/企业所得税估算与申报表核对指引
5. **电子税务局申报并缴款**：按 references/e-tax-bureau.md 完成增值税及附加税费、印花税、个税申报，核对预填数据与台账一致后缴款
6. **留档**：下载完税凭证与申报表 PDF 存到用户指定的归档目录；可用 `ledger.py export --month 2026-09` 导出台账 CSV 一并归档

## 季度 / 年度流程
- **季度**：企业所得税预缴（A 类表）+ 财务报表报送；先用 calc_tax.py 看估算，正式申报以账务数据为准
- **年度（次年 5/31 前）**：企业所得税汇算清缴——引导用户整理全年数据与纳税调整项（业务招待费、广宣费限额等，见 references/taxes.md），申报后多退少补
- **分红**：一人公司向股东分红按 20% 代扣代缴个税（分红次月 15 日内）

## 数据与安全
- 台账默认存 `<skill>/data/ledger.json`；建议用环境变量 `TAX_DATA_DIR` 或 `--data-dir` 指向公司资料目录，避免财务数据混在代码仓库里
- `tax-filing/data/` 已加入 .gitignore；发票号码、客户/供应商名称属敏感信息，切勿提交 git 或上传公开平台
- 金额一律四舍五入到分；税率参数集中在 tools/taxconfig.py

## 口径提示
- 本技能提供流程指导与计算辅助，不替代专业会计意见；政策变化以税务机关最新公告为准
- 2026-01-01《增值税法》施行后申报表表样不变、部分栏次口径调整（国家税务总局公告 2026 年第 6 号），计算口径仍是「销项 − 进项」，不受影响

## 参考资料
- 税种规则与税率：`${CLAUDE_SKILL_DIR}/references/taxes.md`
- 电子税务局操作流程：`${CLAUDE_SKILL_DIR}/references/e-tax-bureau.md`
