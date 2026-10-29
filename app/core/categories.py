"""Reimbursement category rules.

The default taxonomy follows 票据分类.doc. Materials stay in 其他费用 because
that is what the existing reimbursement sheets use.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CATEGORY = "其他费用"

# Bump whenever the built-in rules below change in a way that should apply to
# invoices already in the pool. ``db.init_db`` reclassifies rows whose
# ``category_rule`` names a built-in rule once per version; manual corrections
# and learned seller rules are never overwritten. Version 2 is the hotel-meal
# override (a hotel billing 餐饮服务 is a meal, not lodging), so pools built by
# earlier builds stop keeping those invoices in 差旅费.
RULES_VERSION = 2

# The nine categories used by the reimbursement sheets, in the order they
# appear in 票据分类.doc. The UI offers these as suggestions; custom values
# typed by the user are still accepted.
CATEGORIES: tuple[str, ...] = (
    "汽车费用",
    "差旅费",
    "餐饮费",
    "办公费用",
    "广告费用",
    "培训费",
    "福利费",
    "业务招待费",
    "其他费用",
)

SELLER_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("汽车费用", "seller.fuel", ("中国石油", "中国石化", "壳牌", "加油站")),
    ("汽车费用", "seller.toll", ("高速公路", "交通投资", "路桥")),
    ("差旅费", "seller.hotel", ("酒店", "旅馆", "宾馆")),
    ("餐饮费", "seller.restaurant", ("餐饮", "餐馆", "饭店", "酒楼", "火锅")),
    ("差旅费", "seller.rail", ("铁路", "航空", "机场")),
    ("办公费用", "seller.courier", ("顺丰", "快递", "邮政")),
)

ITEM_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("差旅费", "item.travel", ("住宿", "旅客运输", "客运服务", "铁路", "火车", "机票", "行程单", "打车", "网约车", "代订车", "代驾")),
    ("餐饮费", "item.meal", ("餐饮", "餐费", "餐馆", "饭店", "酒楼", "火锅")),
    ("汽车费用", "item.vehicle", ("汽油", "柴油", "加油", "充电", "停车", "通行费", "过路费", "过桥费", "ETC", "洗车", "轮胎", "维修")),
    ("办公费用", "item.office", ("办公", "文具", "打印", "纸张", "快递", "桶装水", "网络", "电信", "通信费", "中国移动", "移动通信", "中国联通", "中国电信", "U盘", "存储", "数据线", "电脑", "手机")),
    ("广告费用", "item.ad", ("广告", "宣传", "会务", "标签", "瓶身", "设计")),
    ("培训费", "item.training", ("培训", "学费", "教育", "考试")),
    ("福利费", "item.welfare", ("粮油", "大米", "面粉", "食用油", "服装", "鞋帽", "药品", "超市")),
    ("业务招待费", "item.entertainment", ("烟", "酒", "茶叶", "礼品", "娱乐", "旅游")),
    ("其他费用", "item.materials", ("硅藻土", "滤膜", "滤袋", "吨桶", "搅拌桶", "泵", "热缩膜", "打包带", "五金", "配件", "设备", "材料")),
)


@dataclass(frozen=True)
class CategoryResult:
    category: str
    rule: str


# A seller rule describes who sold the invoice; the item describes what was
# bought. For most sellers the former is the better signal, but a hotel that
# bills 餐饮服务 is a meal, not lodging. Only the overrides listed here let the
# item rule win; anything broader would let e.g. an office-supply purchase at a
# gas station escape 汽车费用.
ITEM_OVERRIDES_SELLER: dict[str, frozenset[str]] = {
    "seller.hotel": frozenset({"item.meal"}),
}


def _match(rules: tuple[tuple[str, str, tuple[str, ...]], ...], text: str) -> CategoryResult | None:
    lowered = text.lower()
    for category, rule, keywords in rules:
        if any(keyword.lower() in lowered for keyword in keywords):
            return CategoryResult(category, rule)
    return None


def categorize(
    *,
    item_summary: str,
    seller_name: str,
    file_name: str,
    full_text: str = "",
) -> CategoryResult:
    seller_hit = _match(SELLER_RULES, seller_name)
    item_hit = _match(ITEM_RULES, f"{item_summary} {file_name}")
    if seller_hit:
        overrides = ITEM_OVERRIDES_SELLER.get(seller_hit.rule, frozenset())
        if item_hit is None or item_hit.rule not in overrides:
            return seller_hit
    if item_hit:
        return item_hit

    # Deliberately no whole-page fallback: seller names such as
    # "示例出行旅游有限公司" would otherwise be classified as 业务招待费.
    return CategoryResult(DEFAULT_CATEGORY, "fallback")
