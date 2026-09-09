#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发票台账管理（中国 · 一般纳税人）

维护销项/进项发票与费用台账，数据存 JSON，供 calc_tax.py 计算申报数据。

命令:
  add-sale      记一笔销项发票（按不含税金额自动算税额，或按价税合计拆分）
  add-purchase  记一笔进项发票（金额+税额，或按价税合计拆分；未认证加 --not-certified）
  add-expense   记一笔费用（工资/社保/房租/办公等，用于企业所得税估算）
  list          查看台账（可按月份/类型过滤）
  summary       按月汇总（申报前与电子税务局预填数据核对）
  remove        删除一笔记录
  export        导出某月台账为 CSV（归档用）

数据文件默认 <skill>/data/ledger.json；
可用环境变量 TAX_DATA_DIR（数据目录）或全局 --data-dir 覆盖。
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from taxconfig import CONFIG, q2

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_TYPES = ("sales", "purchases", "expenses")

EXPENSE_CATEGORIES = {
    "salary": "工资薪金",
    "social": "社保公积金(公司承担)",
    "rent": "房租",
    "office": "办公及其他",
    "travel": "差旅",
    "other": "其他",
}


def ledger_path(data_dir=None):
    """台账文件路径：--data-dir > 环境变量 TAX_DATA_DIR > skill 下 data/"""
    if data_dir:
        return os.path.join(data_dir, "ledger.json")
    env = os.environ.get("TAX_DATA_DIR")
    if env:
        return os.path.join(env, "ledger.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "ledger.json")


def load_db(path):
    db = {"sales": [], "purchases": [], "expenses": []}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            db.update(json.load(f))
    return db


def save_db(path, db):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def month_filter(records, month):
    if not month:
        return records
    return [r for r in records if str(r.get("date", "")).startswith(month)]


def disp_width(s):
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(s))


def pad(s, width):
    s = str(s)
    return s + " " * max(0, width - disp_width(s))


def print_table(header, rows):
    widths = []
    for i, h in enumerate(header):
        widths.append(max([disp_width(str(r[i])) for r in rows] + [disp_width(h)]) + 2)
    print("  ".join(pad(h, w) for h, w in zip(header, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(pad(c, w) for c, w in zip(r, widths)))


def fmt(v):
    return f"{v:,.2f}"


def add_common_args(p):
    p.add_argument("--date", help="日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--no", default="", help="发票号码")
    p.add_argument("--desc", default="", help="备注")
    p.add_argument("--rate", type=float, default=CONFIG["vat_rate"] * 100,
                   help="税率%%（默认 {}）".format(CONFIG["vat_rate"] * 100))


def cmd_add_sale(args):
    path = ledger_path(args.data_dir)
    db = load_db(path)
    rate = Decimal(str(args.rate)) / Decimal("100")
    if args.total is not None:
        total = q2(args.total)
        amount = q2(total / (Decimal("1") + rate))
        tax = q2(total - amount)
    else:
        amount = q2(args.amount)
        tax = q2(amount * rate)
        total = q2(amount + tax)
    rec = {
        "date": args.date or datetime.now().strftime("%Y-%m-%d"),
        "no": args.no,
        "customer": args.customer,
        "desc": args.desc,
        "amount": float(amount),
        "tax": float(tax),
        "total": float(total),
        "rate": float(rate),
    }
    db["sales"].append(rec)
    save_db(path, db)
    print("[销项] %s %s 不含税 %s 税额 %s 价税合计 %s" % (
        rec["date"], rec["customer"], fmt(amount), fmt(tax), fmt(total)))
    print("已写入 %s" % path)


def cmd_add_purchase(args):
    path = ledger_path(args.data_dir)
    db = load_db(path)
    rate = Decimal(str(args.rate)) / Decimal("100")
    if args.total is not None:
        total = q2(args.total)
        amount = q2(total / (Decimal("1") + rate))
        tax = q2(total - amount)
    else:
        amount = q2(args.amount)
        tax = q2(args.tax)
        total = q2(amount + tax)
    rec = {
        "date": args.date or datetime.now().strftime("%Y-%m-%d"),
        "no": args.no,
        "vendor": args.vendor,
        "desc": args.desc,
        "amount": float(amount),
        "tax": float(tax),
        "total": float(total),
        "rate": float(rate),
        "deductible": not args.not_deductible,
        "certified": not args.not_certified,
    }
    db["purchases"].append(rec)
    save_db(path, db)
    flags = []
    if not rec["deductible"]:
        flags.append("不可抵扣")
    if not rec["certified"]:
        flags.append("未认证")
    print("[进项] %s %s 不含税 %s 税额 %s 价税合计 %s %s" % (
        rec["date"], rec["vendor"], fmt(amount), fmt(tax), fmt(total),
        ("(" + "/".join(flags) + ")") if flags else ""))
    print("已写入 %s" % path)


def cmd_add_expense(args):
    path = ledger_path(args.data_dir)
    db = load_db(path)
    rec = {
        "date": args.date or datetime.now().strftime("%Y-%m-%d"),
        "item": args.item,
        "category": args.category,
        "amount": float(q2(args.amount)),
    }
    db["expenses"].append(rec)
    save_db(path, db)
    print("[费用] %s %s(%s) %s" % (
        rec["date"], rec["item"], EXPENSE_CATEGORIES.get(rec["category"], rec["category"]), fmt(rec["amount"])))
    print("已写入 %s" % path)


def cmd_list(args):
    path = ledger_path(args.data_dir)
    db = load_db(path)
    types = [args.type] if args.type else list(DEFAULT_TYPES)
    for t in types:
        records = month_filter(db[t], args.month)
        print("[%s] %s 条记录" % (t, len(records)))
        if not records:
            continue
        if t == "sales":
            header = ["#", "日期", "客户", "发票号", "不含税", "税额", "价税合计"]
            rows = [[i, r["date"], r.get("customer", ""), r.get("no", ""),
                     fmt(r["amount"]), fmt(r["tax"]), fmt(r["total"])]
                    for i, r in enumerate(records)]
        elif t == "purchases":
            header = ["#", "日期", "供应商", "发票号", "不含税", "税额", "价税合计", "状态"]
            rows = [[i, r["date"], r.get("vendor", ""), r.get("no", ""),
                     fmt(r["amount"]), fmt(r["tax"]), fmt(r["total"]),
                     "可抵扣" if r.get("deductible", True) and r.get("certified", True) else
                     ("未认证" if not r.get("certified", True) else "不可抵扣")]
                    for i, r in enumerate(records)]
        else:
            header = ["#", "日期", "事项", "类别", "金额"]
            rows = [[i, r["date"], r.get("item", ""),
                     EXPENSE_CATEGORIES.get(r.get("category", "other"), r.get("category", "other")),
                     fmt(r["amount"])]
                    for i, r in enumerate(records)]
        print_table(header, rows)
        print()


def cmd_summary(args):
    path = ledger_path(args.data_dir)
    db = load_db(path)
    month = args.month or datetime.now().strftime("%Y-%m")
    sales = month_filter(db["sales"], month)
    purchases = month_filter(db["purchases"], month)
    expenses = month_filter(db["expenses"], month)
    if not (sales or purchases or expenses):
        print("%s 台账无记录，先用 add-sale / add-purchase / add-expense 记账" % month)
        return

    def total(records, key):
        return sum((Decimal(str(r[key])) for r in records), Decimal("0"))

    s_amt, s_tax, s_total = total(sales, "amount"), total(sales, "tax"), total(sales, "total")
    p_amt, p_tax = total(purchases, "amount"), total(purchases, "tax")
    certified = [r for r in purchases if r.get("deductible", True) and r.get("certified", True)]
    p_deduct = total(certified, "tax")
    e_total = total(expenses, "amount")

    print("==== %s 台账汇总 ====" % month)
    print("销项: %d 笔 | 不含税金额 %s | 销项税额 %s | 价税合计 %s" % (
        len(sales), fmt(s_amt), fmt(s_tax), fmt(s_total)))
    print("进项: %d 笔 | 不含税金额 %s | 税额 %s" % (len(purchases), fmt(p_amt), fmt(p_tax)))
    print("      其中可抵扣(已认证): %d 笔 | 税额 %s" % (len(certified), fmt(p_deduct)))
    if expenses:
        print("费用: %d 笔 | 合计 %s" % (len(expenses), fmt(e_total)))
        by_cat = {}
        for r in expenses:
            cat = r.get("category", "other")
            by_cat[cat] = by_cat.get(cat, Decimal("0")) + Decimal(str(r["amount"]))
        for cat, v in by_cat.items():
            print("      - %s: %s" % (EXPENSE_CATEGORIES.get(cat, cat), fmt(v)))
    if s_tax - p_deduct < 0:
        print("提示: 进项大于销项，当期应纳增值税为 0，留抵税额 %s 结转下期" % fmt(p_deduct - s_tax))


def cmd_remove(args):
    path = ledger_path(args.data_dir)
    db = load_db(path)
    records = db[args.type]
    if not (0 <= args.id < len(records)):
        print("无效 id: %d（%s 共 %d 条，id 从 0 开始）" % (args.id, args.type, len(records)))
        sys.exit(1)
    rec = records.pop(args.id)
    save_db(path, db)
    print("已删除 [%s#%d] %s" % (args.type, args.id, json.dumps(rec, ensure_ascii=False)))


def cmd_export(args):
    path = ledger_path(args.data_dir)
    db = load_db(path)
    month = args.month or datetime.now().strftime("%Y-%m")
    out_dir = args.out or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    specs = {
        "sales": ["date", "no", "customer", "desc", "amount", "tax", "total", "rate"],
        "purchases": ["date", "no", "vendor", "desc", "amount", "tax", "total", "rate",
                      "deductible", "certified"],
        "expenses": ["date", "item", "category", "amount"],
    }
    for t, fields in specs.items():
        records = month_filter(db[t], month)
        out = os.path.join(out_dir, "%s-%s.csv" % (t, month))
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(records)
        print("已导出 %d 条 → %s" % (len(records), out))


def main():
    ap = argparse.ArgumentParser(description="发票台账管理（中国 · 一般纳税人）")
    ap.add_argument("--data-dir", help="台账数据目录（默认 skill 下 data/，可用环境变量 TAX_DATA_DIR）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-sale", help="记一笔销项发票")
    add_common_args(p)
    p.add_argument("--customer", required=True, help="客户名称")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--amount", type=float, help="不含税金额")
    g.add_argument("--total", type=float, help="价税合计（自动拆分不含税额与税额）")
    p.set_defaults(func=cmd_add_sale)

    p = sub.add_parser("add-purchase", help="记一笔进项发票")
    add_common_args(p)
    p.add_argument("--vendor", required=True, help="供应商名称")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--amount", type=float, help="不含税金额（需配合 --tax）")
    g.add_argument("--total", type=float, help="价税合计（自动拆分不含税额与税额）")
    p.add_argument("--tax", type=float, help="税额（--amount 模式下必填）")
    p.add_argument("--not-deductible", action="store_true", help="不可抵扣（如餐费、个人消费）")
    p.add_argument("--not-certified", action="store_true", help="尚未勾选认证（当期不计入抵扣）")
    p.set_defaults(func=cmd_add_purchase)

    p = sub.add_parser("add-expense", help="记一笔费用（企业所得税估算用）")
    p.add_argument("--date", help="日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--item", required=True, help="费用事项，如 工资/房租")
    p.add_argument("--amount", type=float, required=True, help="金额（元）")
    p.add_argument("--category", choices=list(EXPENSE_CATEGORIES), default="other",
                   help="费用类别（默认 other）")
    p.set_defaults(func=cmd_add_expense)

    p = sub.add_parser("list", help="查看台账")
    p.add_argument("--month", help="只显示该月份 YYYY-MM")
    p.add_argument("--type", choices=list(DEFAULT_TYPES), help="只显示该类型")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("summary", help="按月汇总（申报前核对用）")
    p.add_argument("--month", help="月份 YYYY-MM（默认当月）")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("remove", help="删除一笔记录")
    p.add_argument("--type", required=True, choices=list(DEFAULT_TYPES), help="记录类型")
    p.add_argument("--id", type=int, required=True, help="记录 id（list 里的 # 列，从 0 开始）")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("export", help="导出某月台账为 CSV（归档用）")
    p.add_argument("--month", help="月份 YYYY-MM（默认当月）")
    p.add_argument("--out", help="输出目录（默认当前目录）")
    p.set_defaults(func=cmd_export)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
