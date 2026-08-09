"""排行表欄位組的契約：每一組都必須含身分欄。

mispriced 組當初漏了代號／名稱／收盤。在「切榜自動帶到對應欄位組」上線前
沒人點到那組所以沒被發現；上線後選錯殺價值榜會自動跳過去，整張表變成
只有分數認不出是哪一檔股票。

這個測試直接解析 gui.py 裡的 rankingCols / columnGroups / RANKING_COLUMN_GROUP
三份定義，任何新增的欄位組或自動切換對照若漏了身分欄就會失敗。
"""
import re

from stock_chip.gui import INDEX_HTML

# 沒有這三欄就認不出是哪一檔股票，任何欄位組都不能缺
IDENTITY_COLUMNS = {"stock_id", "name", "close"}


def _ranking_columns() -> list[tuple[str, str, set[str]]]:
    start = INDEX_HTML.index("const rankingCols = [")
    end = INDEX_HTML.index("\n    ];", start)
    block = INDEX_HTML[start:end]
    found = re.findall(r'\{key:"([a-z0-9_]+)", label:"([^"]+)"[^}]*?groups:\[([^\]]*)\]', block)
    return [
        (key, label, {g.strip().strip('"') for g in groups.split(",") if g.strip()})
        for key, label, groups in found
    ]


def _declared_groups() -> set[str]:
    start = INDEX_HTML.index("const columnGroups = [")
    end = INDEX_HTML.index("\n    ];", start)
    return set(re.findall(r'\{key:"(\w+)"', INDEX_HTML[start:end]))


def _auto_switch_map() -> dict[str, str]:
    block = re.search(r"const RANKING_COLUMN_GROUP = \{(.*?)\};", INDEX_HTML, re.S).group(1)
    return dict(re.findall(r'(\w+):\s*"(\w+)"', block))


def test_every_column_group_includes_identity_columns():
    columns = _ranking_columns()
    for group in sorted(_declared_groups()):
        present = {key for key, _label, groups in columns if group in groups}
        missing = IDENTITY_COLUMNS - present
        assert not missing, (
            f"欄位組「{group}」缺少身分欄 {sorted(missing)}——"
            f"表格會只剩分數，認不出是哪一檔股票"
        )


def test_auto_switch_targets_are_real_groups():
    """切榜自動帶到的欄位組必須真的存在，否則 visibleRankingCols 會濾成空表。"""
    declared = _declared_groups()
    for ranking, group in _auto_switch_map().items():
        assert group in declared, f"{ranking} 指向不存在的欄位組「{group}」"


def test_identity_columns_are_declared_at_all():
    keys = {key for key, _label, _groups in _ranking_columns()}
    assert IDENTITY_COLUMNS <= keys, f"rankingCols 缺少身分欄定義：{sorted(IDENTITY_COLUMNS - keys)}"
