"""ETF 資金流模組：名單篩選、分類與變化率的回歸測試。"""
from stock_chip.etf import build_rows, classify_category, is_domestic_equity, units_change_pct


def test_is_domestic_equity_filters():
    assert is_domestic_equity("國內成分證券指數股票型基金(股票)", 0, "元大台灣50")
    assert not is_domestic_equity("國內成分證券指數股票型基金(股票)", 1, "含國外")  # 含國外成分
    assert not is_domestic_equity("指數股票型期貨信託基金", 0, "街口布蘭特原油")  # 期貨型
    assert not is_domestic_equity("國內成分證券指數股票型基金(債券)", 0, "元大美債")  # 非股票
    assert not is_domestic_equity("國內成分證券指數股票型基金(股票)", 0, "元大台灣50反向1")  # 反向


def test_classify_category():
    assert classify_category("元大高股息", "臺灣高股息指數") == "高股息"
    assert classify_category("國泰台灣科技龍頭", "臺灣科技指數") == "科技"
    assert classify_category("元大台灣50", "富時臺灣50指數") == "市值型"
    assert classify_category("主動國泰動能高息", "不適用") == "高股息"  # 高息優先於主動


def test_build_rows_hot_selection():
    meta = [
        {"etf_id": f"00{i:02d}", "name": f"台股ETF{i}", "fund_type": "國內成分證券指數股票型基金(股票)",
         "has_foreign": 0, "units": float(1000 - i), "data_date": "2026-07-22"}
        for i in range(30)
    ] + [
        {"etf_id": "0099B", "name": "債券ETF", "fund_type": "指數股票型基金(債券)",
         "has_foreign": 0, "units": 99999.0, "data_date": "2026-07-22"},
    ]
    rows = build_rows(meta, {}, {})
    hot = [row for row in rows if row.is_hot]
    assert len(hot) == 20  # 前 20 檔
    assert all(row.etf_id != "0099B" for row in hot)  # 債券型不入榜（即使單位數最大）


def test_units_change_pct():
    assert units_change_pct(110.0, 100.0) == 10.0
    assert units_change_pct(None, 100.0) is None
    assert units_change_pct(100.0, 0) is None
