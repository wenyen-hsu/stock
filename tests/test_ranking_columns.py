"""排行表欄位組的契約：每一組都必須含身分欄。

mispriced 組當初漏了代號／名稱／收盤。在「切榜自動帶到對應欄位組」上線前
沒人點到那組所以沒被發現；上線後選錯殺價值榜會自動跳過去，整張表變成
只有分數認不出是哪一檔股票。

這個測試解析 gui.py 裡的 rankingCols / columnGroups / RANKING_COLUMN_GROUP
三份定義。因為是對「原始碼字串」做正則，有兩個自身的失效風險，下面都處理了：

1. 排版敏感：`groups:[...]` 改成 `groups: [...]` 就會抓不到。正則一律用 \\s* 吃空白。
2. 解析失敗會靜默通過：抓不到任何欄位時 for 迴圈不執行，測試照樣綠。
   所以每個解析函式都先斷言「有抓到東西」，解析壞掉要當成測試失敗而不是通過。
"""
import re

import pytest

from stock_chip.gui import INDEX_HTML

# 沒有這三欄就認不出是哪一檔股票，任何欄位組都不能缺
IDENTITY_COLUMNS = {"stock_id", "name", "close"}

# 解析結果的合理下限——低於這些數字幾乎肯定是正則失效而不是真的變少
MIN_COLUMNS = 40
MIN_GROUPS = 5
MIN_AUTO_SWITCH = 5


def _array_block(declaration: str) -> str:
    """取出 `const <name> = [ ... ];` / `= { ... };` 的內容，不依賴縮排。"""
    match = re.search(
        r"const\s+" + declaration + r"\s*=\s*[\[{](.*?)[\]}]\s*;",
        INDEX_HTML,
        re.S,
    )
    assert match, f"在 gui.py 找不到 {declaration} 的定義——解析失效，不是契約通過"
    return match.group(1)


def _ranking_columns() -> list[tuple[str, set[str]]]:
    block = _array_block("rankingCols")
    found = re.findall(
        r'\{\s*key:\s*"([a-z0-9_]+)"[^{}]*?groups:\s*\[([^\]]*)\]',
        block,
    )
    columns = [
        (key, {g.strip().strip('"\'') for g in groups.split(",") if g.strip()})
        for key, groups in found
    ]
    assert len(columns) >= MIN_COLUMNS, (
        f"只解析到 {len(columns)} 個帶 groups 的欄位（預期 ≥{MIN_COLUMNS}）——"
        f"正則可能被排版變動打壞了，請修正則而不是調低門檻"
    )
    return columns


def _declared_groups() -> set[str]:
    block = _array_block("columnGroups")
    groups = set(re.findall(r'\{\s*key:\s*"(\w+)"', block))
    assert len(groups) >= MIN_GROUPS, (
        f"只解析到 {len(groups)} 個欄位組（預期 ≥{MIN_GROUPS}）——正則可能失效"
    )
    return groups


def _auto_switch_map() -> dict[str, str]:
    block = _array_block("RANKING_COLUMN_GROUP")
    mapping = dict(re.findall(r'(\w+)\s*:\s*"(\w+)"', block))
    assert len(mapping) >= MIN_AUTO_SWITCH, (
        f"只解析到 {len(mapping)} 筆自動切換對照（預期 ≥{MIN_AUTO_SWITCH}）——正則可能失效"
    )
    return mapping


def test_every_column_group_includes_identity_columns():
    columns = _ranking_columns()
    for group in sorted(_declared_groups()):
        present = {key for key, groups in columns if group in groups}
        missing = IDENTITY_COLUMNS - present
        assert not missing, (
            f"欄位組「{group}」缺少身分欄 {sorted(missing)}——"
            f"表格會只剩分數，認不出是哪一檔股票"
        )


def test_auto_switch_targets_are_real_groups():
    """切榜自動帶到的欄位組必須真的存在，否則 visibleRankingCols 會濾成空表。"""
    declared = _declared_groups()
    for ranking, group in sorted(_auto_switch_map().items()):
        assert group in declared, f"{ranking} 指向不存在的欄位組「{group}」"


def test_identity_columns_are_declared_at_all():
    keys = {key for key, _groups in _ranking_columns()}
    assert IDENTITY_COLUMNS <= keys, f"rankingCols 缺少身分欄定義：{sorted(IDENTITY_COLUMNS - keys)}"


@pytest.mark.parametrize(
    "variant",
    [
        '{key:"name", label:"名稱", groups:["core","chip"]},',       # 現行寫法
        '{ key: "name", label: "名稱", groups: ["core", "chip"] },',  # 加空白重排
        "{key:'name', label:'名稱', groups:['core','chip']},",        # 單引號
    ],
)
def test_column_regex_tolerates_reformatting(variant):
    """純排版變動不該讓契約測試誤判。正則若太嚴，改個縮排就會假性失敗。"""
    found = re.findall(r'\{\s*key:\s*[\'"]([a-z0-9_]+)[\'"][^{}]*?groups:\s*\[([^\]]*)\]', variant)
    assert found, f"正則對這種寫法失效：{variant}"
    key, groups = found[0]
    assert key == "name"
    assert {g.strip().strip('"\'') for g in groups.split(",")} == {"core", "chip"}
