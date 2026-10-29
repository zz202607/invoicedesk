from __future__ import annotations

from app.core.categories import categorize


def test_seller_rule_wins_over_item_text() -> None:
    result = categorize(
        item_summary="92号 车用汽油",
        seller_name="示例加油站",
        file_name="sample-001.pdf",
    )
    assert result.category == "汽车费用"
    assert result.rule == "seller.fuel"


def test_travel_company_name_does_not_trigger_entertainment() -> None:
    result = categorize(
        item_summary="代订车服务费",
        seller_name="示例出行旅游有限公司",
        file_name="sample-002.pdf",
    )
    assert result.category == "差旅费"


def test_materials_are_other_expenses() -> None:
    result = categorize(
        item_summary="硅藻土助滤剂",
        seller_name="示例建材商行",
        file_name="sample-003.pdf",
    )
    assert result.category == "其他费用"


def test_telecom_is_office_expense() -> None:
    result = categorize(
        item_summary="通信费",
        seller_name="示例电信分公司",
        file_name="sample-004.pdf",
    )
    assert result.category == "办公费用"


def test_mobile_ac_does_not_match_mobile_telecom() -> None:
    result = categorize(
        item_summary="移动空调",
        seller_name="示例电器公司",
        file_name="sample-005.pdf",
    )
    assert result.category == "其他费用"


def test_hotel_meal_item_beats_hotel_seller() -> None:
    result = categorize(
        item_summary="餐饮服务",
        seller_name="示例酒店管理有限公司",
        file_name="sample-007.pdf",
    )
    assert result.category == "餐饮费"
    assert result.rule == "item.meal"


def test_hotel_lodging_item_stays_travel() -> None:
    result = categorize(
        item_summary="住宿服务",
        seller_name="示例酒店管理有限公司",
        file_name="sample-008.pdf",
    )
    assert result.category == "差旅费"
    assert result.rule == "seller.hotel"


def test_unknown_falls_back_to_other() -> None:
    result = categorize(item_summary="净水器", seller_name="某商行", file_name="sample-006.pdf")
    assert result.category == "其他费用"
    assert result.rule == "fallback"
