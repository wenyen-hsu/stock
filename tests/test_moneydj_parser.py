"""MoneyDJ 分點頁解析的回歸測試。

fixtures 取自 2026-07 診斷時的真實回應（縮減列數）；來源改版時這裡會先亮紅燈。
"""
from pathlib import Path

from stock_chip.branch import (
    decode_broker_id,
    encode_broker_id,
    moneydj_date,
    normalize_date,
    parse_moneydj_zco,
    parse_number,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_zco_interval_table():
    rows = parse_moneydj_zco(
        load_fixture("zco_sample.html"),
        source_url="http://test",
        stock_id="2330",
        stock_name="台積電",
        from_date="2026-06-02",
        to_date="2026-06-30",
        window_days=20,
        top_n=10,
    )
    assert len(rows) == 4  # 2 buy + 2 sell
    buy1 = next(r for r in rows if r.rank_side == "buy" and r.rank_no == 1)
    assert buy1.broker_name == "元大證券"
    assert buy1.broker_id == "9800"
    assert buy1.buy_lot == 81152 and buy1.sell_lot == 52670 and buy1.net_lot == 28482
    assert buy1.avg_price is None  # MoneyDJ 無均價欄位
    sell1 = next(r for r in rows if r.rank_side == "sell" and r.rank_no == 1)
    assert sell1.broker_name == "台灣摩根士丹利"
    assert sell1.net_lot == 28657
    # UTF-16BE 十六進位分點代號要解碼
    buy2 = next(r for r in rows if r.rank_side == "buy" and r.rank_no == 2)
    assert buy2.broker_id == "9A00"


def test_parse_zco_top_n_limit():
    rows = parse_moneydj_zco(
        load_fixture("zco_sample.html"),
        source_url="http://test",
        stock_id="2330",
        stock_name="台積電",
        from_date="2026-06-02",
        to_date="2026-06-30",
        window_days=20,
        top_n=1,
    )
    assert len(rows) == 2
    assert {r.rank_no for r in rows} == {1}


def test_parse_zco_skips_footer_rows():
    rows = parse_moneydj_zco(
        load_fixture("zco_sample.html"),
        source_url="http://test",
        stock_id="2330",
        stock_name="台積電",
        from_date="2026-06-02",
        to_date="2026-06-30",
        window_days=20,
        top_n=10,
    )
    names = {r.broker_name for r in rows}
    assert "合計買超張數" not in names


def test_zco0_daily_fixture_shape():
    """zco0 單一分點頁的列格式（5 欄、t4n0 日期、帶正負號買賣超）。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(load_fixture("zco0_sample.html"), "html.parser")
    data_rows = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) != 5 or "t4n0" not in (cells[0].get("class") or []):
            continue
        data_rows.append([cell.get_text(strip=True) for cell in cells])
    assert len(data_rows) == 3
    assert normalize_date(data_rows[0][0]) == "2026-07-01"
    assert parse_number(data_rows[2][4]) == -1636


def test_broker_id_codec():
    assert decode_broker_id("0039004100380031") == "9A81"
    assert decode_broker_id("9800") == "9800"
    assert decode_broker_id(None) is None
    assert encode_broker_id("9A81") == "0039004100380031"
    assert encode_broker_id("9800") == "9800"


def test_moneydj_date_format():
    assert moneydj_date("2026-06-02") == "2026-6-2"
    assert moneydj_date("2026/12/31") == "2026-12-31"
