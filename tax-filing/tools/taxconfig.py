# -*- coding: utf-8 -*-
"""税率与申报参数配置（公司信息或税率变化时改这里）。"""
from decimal import Decimal, ROUND_HALF_UP

CONFIG = {
    # 增值税
    "vat_rate": 0.06,            # 销项税率（现代服务 6%）；同时是台账记账默认税率
    # 附加税费（计税依据 = 实际缴纳的增值税）
    "city_rate": 0.07,           # 城建税：市区 7% / 县城镇 5% / 其他 1%
    "edu_rate": 0.03,            # 教育费附加
    "local_edu_rate": 0.02,      # 地方教育附加
    "small_profit": True,        # 小型微利企业 → 附加税费减半 + 企业所得税 5%
    "surcharge_half": 0.5,       # 六税两费减半比例（公告 2023 年第 12 号，至 2027-12-31）
    # 企业所得税
    "cit_normal_rate": 0.25,     # 法定税率
    "cit_small_rate": 0.05,      # 小微实际税负（减按 25% 计入 × 20% 税率）
    "cit_small_limit": 3000000,  # 小微应纳税所得额上限（「335」标准之 300 万）
    # 印花税
    "stamp_tech_rate": 0.0003,   # 技术合同 万分之三
    "stamp_trade_rate": 0.0003,  # 买卖合同 万分之三
}

R2 = Decimal("0.01")


def q2(value):
    """金额四舍五入到分。"""
    return Decimal(str(value)).quantize(R2, rounding=ROUND_HALF_UP)
