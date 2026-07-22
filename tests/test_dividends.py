"""除權息模組：民國日期、金額解析與權息拆解的回歸測試。"""
from stock_chip.dividends import parse_roc_date, split_components, to_number


def test_parse_roc_date_variants():
    assert parse_roc_date("115年06月01日") == "2026-06-01"
    assert parse_roc_date("1150713") == "2026-07-13"
    assert parse_roc_date("115/7/1") == "2026-07-01"
    assert parse_roc_date("") is None
    assert parse_roc_date("115年13月01日") is None  # 無效月份


def test_split_components_cash_only():
    # 純息：減除股利參考價 == 除權息參考價 → 配股率 0
    cash, ratio = split_components(57.70, 55.50, 55.50)
    assert cash == 2.2
    assert ratio == 0.0


def test_split_components_rights_only():
    # 純權：減除股利參考價 == 前收盤（無現金）→ 現金 0
    cash, ratio = split_components(50.0, 45.4545, 50.0)
    assert cash == 0.0
    assert abs(ratio - 0.1) < 1e-3  # 每股配 0.1 股


def test_split_components_mixed():
    # 權息並行：前收 55、現金 2 → 減除股利參考價 53；再配 0.1 股 → 參考價 53/1.1
    cash, ratio = split_components(55.0, 53.0 / 1.1, 53.0)
    assert cash == 2.0
    assert abs(ratio - 0.1) < 1e-6


def test_split_components_missing():
    assert split_components(None, 55.5, 55.5) == (None, None)
    assert split_components(57.7, None, 55.5) == (None, None)


def test_to_number():
    assert to_number("1,234.5") == 1234.5
    assert to_number("--") is None
    assert to_number("") is None
    assert to_number("0.270000") == 0.27
