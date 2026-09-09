#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""申报数据计算（中国 · 一般纳税人）

按台账（ledger.json）或手工参数计算当月申报数据：
应纳增值税、附加税费、印花税参考、企业所得税预缴估算，并给出申报表核对指引。

用法:
  python calc_tax.py --month 2026-09                                  # 按台账
  python calc_tax.py --sales-amount 100000 --purchase-tax 3000        # 手工
"""

import argparse
import os
import sys
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from taxconfig import CONFIG, q2
from ledger import ledger_path, load_db, month_filter

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def d(v):
    return Decimal(str(v))


def fmt(v):
    return f"{v:,.2f}"


def main():
    ap = argparse.ArgumentParser(description="申报数据计算（中国 · 一般纳税人）")
    ap.add_argument("--month", help="申报月份 YYYY-MM（默认当月）")
    ap.add_argument("--data-dir", help="台账数据目录（默认 skill 下 data/）")
    ap.add_argument("--not-small-profit", action="store_true",
                    help="不按小型微利企业（企业所得税 25%%，附加税费不减半）")
    ap.add_argument("--sales-amount", type=float, help="[手工] 当月销项不含税销售额")
    ap.add_argument("--sales-tax", type=float, help="[手工] 当月销项税额")
    ap.add_argument("--purchase-tax", type=float, help="[手工] 当月已认证可抵扣进项税额")
    ap.add_argument("--expense-total", type=float, help="[手工] 当月费用合计（企业所得税估算用）")
    args = ap.parse_args()

    month = args.month or datetime.now().strftime("%Y-%m")
    path = ledger_path(args.data_dir)
    db = load_db(path)
    sales = month_filter(db["sales"], month)
    purchases = month_filter(db["purchases"], month)
    expenses = month_filter(db["expenses"], month)

    manual = any(v is not None for v in (args.sales_amount, args.sales_tax, args.purchase_tax))
    if manual:
        source = "手工输入"
        sales_amount = q2(args.sales_amount or 0)
        sales_tax = d(args.sales_tax) if args.sales_tax is not None \
            else q2(sales_amount * d(CONFIG["vat_rate"]))
        purchase_tax = q2(args.purchase_tax or 0)
        purchase_tax_all = purchase_tax
        expense_total = q2(args.expense_total or 0)
        purchase_amount = Decimal("0")
    else:
        if not (sales or purchases or expenses):
            print("%s 台账中无任何记录。请先用 ledger.py 记账，或用 --sales-amount/--purchase-tax 等手工参数。" % month)
            sys.exit(1)
        source = "台账(%s)" % path
        sales_amount = sum((d(r["amount"]) for r in sales), Decimal("0"))
        sales_tax = sum((d(r["tax"]) for r in sales), Decimal("0"))
        purchase_tax = sum((d(r["tax"]) for r in purchases
                            if r.get("deductible", True) and r.get("certified", True)), Decimal("0"))
        purchase_tax_all = sum((d(r["tax"]) for r in purchases), Decimal("0"))
        purchase_amount = sum((d(r["amount"]) for r in purchases), Decimal("0"))
        expense_total = sum((d(r["amount"]) for r in expenses), Decimal("0"))

    small = CONFIG["small_profit"] and not args.not_small_profit
    vat = sales_tax - purchase_tax

    print(f"==== {month} 申报数据单（科技有限公司 · 一般纳税人 · 现代服务 6%）====")
    print("数据来源: %s" % source)
    print()
    print("一、增值税（按月申报）")
    print("  销项税额      : %s" % fmt(sales_tax))
    print("  可抵扣进项税额: %s" % fmt(purchase_tax))
    if purchase_tax_all > purchase_tax:
        print("    （台账进项合计 %s，其中未认证/不可抵扣 %s 未计入）" % (
            fmt(purchase_tax_all), fmt(purchase_tax_all - purchase_tax)))
    if vat >= 0:
        print("  应纳增值税    : %s  (= 销项 - 进项)" % fmt(vat))
    else:
        print("  应纳增值税    : 0.00")
        print("  期末留抵税额  : %s  (结转下期继续抵扣)" % fmt(-vat))
    print()
    print("二、附加税费（随增值税申报）")
    if vat > 0:
        half = d(CONFIG["surcharge_half"]) if small else d("1")
        city = q2(vat * d(CONFIG["city_rate"]) * half)
        edu = q2(vat * d(CONFIG["edu_rate"]) * half)
        local_edu = q2(vat * d(CONFIG["local_edu_rate"]) * half)
        surcharge_total = q2(city + edu + local_edu)
        tag = "  [小型微利企业已减半]" if small else ""
        print(f"  城建税({CONFIG['city_rate'] * 100:g}%)      : {fmt(city)}")
        print(f"  教育费附加(3%)  : {fmt(edu)}")
        print(f"  地方教育附加(2%): {fmt(local_edu)}")
        print(f"  附加税费合计    : {fmt(surcharge_total)}{tag}")
    else:
        print("  当期无应纳增值税，附加税费为 0")
    print()
    print("三、印花税（按季申报，参考值）")
    stamp = q2(sales_amount * d(CONFIG["stamp_tech_rate"]))
    print(f"  技术合同(收入×0.03%) ≈ {fmt(stamp)}  ← 以实际合同金额为准；买卖合同同为 0.03%")
    print()
    print("四、企业所得税（按季预缴，估算）")
    cost = purchase_amount + expense_total
    if manual and not args.expense_total:
        print("  注意: 未提供费用数据，估算偏差较大")
    profit = sales_amount - cost
    if small:
        cit = q2(profit * d(CONFIG["cit_small_rate"])) if profit > 0 else Decimal("0")
        print(f"  估算应纳税所得额 = 收入 {fmt(sales_amount)} - 成本费用 {fmt(cost)} = {fmt(profit)}")
        print(f"  按小型微利企业 5% 估算应纳税额 ≈ {fmt(cit)}")
        print("  提醒: 全年应纳税所得额超过 300 万时不再适用 5%，全额按 25%（300 万≈15 万，301 万≈75.25 万）")
    else:
        cit = q2(profit * d(CONFIG["cit_normal_rate"])) if profit > 0 else Decimal("0")
        print("  估算应纳税所得额 ≈ %s" % fmt(profit))
        print(f"  按 25% 估算应纳税额 ≈ {fmt(cit)}")
    print("  注: 仅按台账估算（成本费用按当月口径），正式申报以账务数据与电子税务局预填为准")
    print()
    print("五、申报表核对指引（电子税务局已预填，逐栏核对）")
    print("  附表一(本期销售情况明细): 6% 税率行 → 销售额/销项税额")
    print("  附表二(本期进项税额明细): 认证相符的进项税额")
    print("  主表: 第1栏销售额 / 第11栏销项税额 / 第12栏进项税额 / 第19、24栏应纳税额")
    print("  附加税费申报表: 计税依据 = 主表应纳税额（小微减半自动享受）")
    print()
    print("六、截止提醒")
    print("  增值税/附加税费/个税代扣: 次月 15 日前（遇节假日顺延）")
    print("  企业所得税预缴（本季度）: 季度终了后 15 日内")


if __name__ == "__main__":
    main()
