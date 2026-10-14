"""
このテストが守るもの: (1) 補完元がDXFにもある場合、構成数不足の余り`X?`が別行に
ならず本体行のULKES個数へ合算されること、(2) `T1`等の「英字1字+数字1〜2桁」が
ULKES側に同じ英字で始まる符号（T01等）があっても比較に現れないこと、(3) 全区分が
淡色で、個数一致の「両方」にも色が付くこと。

不具合の識別子: 2026-10-14 ユーザー報告（EE6312-000-01A）
    (1)「CNCB001W」が両方、「CNCB001W?」がULKESのみと2行に分かれて見づらい。
    (2)「図面のみ」にT1が出る（DXF-extract-labelsでは機器符号候補から除外済み）。

以前どう壊れていたか:
    (1) 振替しきれなかった余りを無条件に`{source}?`行にしていた。
    (2) HostPL-comparerの除外パターンにOTHER_PATTERNSが無く、さらに
    `rescue_by_ulkes_prefix()`が英字プレフィックスだけで救済するため、除外しても
    ULKESに`T01`があるだけで`T1`が復活する構造だった。

修正後に保証したいこと:
    - 補完元がDXFに無いときだけ`X?`行が残る。
    - `T1`は表に出ず、ULKESに完全一致する`T01`は「両方」になる。
    - C1/Q2/U3/F1（A・C・L・Q・U・F始まり）は従来どおり候補。

実行:
    cd HostPL-comparer
    python -m pytest tests/regression/bugfix/test_ulkes_q_leftover_merged_and_t1_excluded.py
"""
from collections import Counter

import pytest

from model.compare_symbols import (
    ROW_STYLE_COLORS,
    compare_pair,
)
from model.label_classifier import is_ref_designator_label as is_ref_designator_candidate


def test_leftover_merged_into_source_row_when_source_in_dxf():
    result = compare_pair(Counter({"CNCB001W": 1}), ["CNCB001W"] + [f"CNCB001W?{i:03d}" for i in range(1, 7)])
    df = result["symbol_df"]
    assert df["符号"].tolist() == ["CNCB001W"]
    row = df.iloc[0]
    assert (row["区分"], row["図面個数"], row["ULKES個数"]) == ("両方", 1, 7)


def test_leftover_row_kept_when_source_not_in_dxf():
    result = compare_pair(Counter({"OTHER1": 1}), ["CNCB001W", "CNCB001W?001"])
    df = result["symbol_df"]
    leftover = df[df["符号"] == "CNCB001W?"].iloc[0]
    assert leftover["区分"] == "ULKESのみ"


def test_t1_not_rescued_by_prefix_but_exact_match_is_rescued():
    result = compare_pair(
        Counter(), ["T01", "TB005"], rejected_counter=Counter({"T1": 2, "T01": 2, "TB005 (30A)": 1}),
    )
    labels = result["symbol_df"]["符号"].tolist()
    assert "T1" not in labels
    assert "T01" in labels
    assert "TB005" in labels  # 除外カテゴリ外は従来どおりプレフィックス救済
    assert "T1" not in result["dxf_display_counter"]


@pytest.mark.parametrize("label, expected", [
    ("T1", False), ("R10", False), ("N24", False), ("R49(1KΩ)", False), ("T01", False),
    ("C1", True), ("Q2", True), ("U3", True), ("F1", True), ("R001", True), ("CB004A", True),
])
def test_short_letter_digits_candidate_judgment(label, expected):
    assert is_ref_designator_candidate(label) is expected


def test_all_row_styles_have_light_colors():
    assert set(ROW_STYLE_COLORS) == {"DXF_ONLY", "ULKES_ONLY", "MISMATCH", "MATCH"}
    assert ROW_STYLE_COLORS["MATCH"]["bg_color"] == "#EAF7EC"
    assert ROW_STYLE_COLORS["MISMATCH"]["bg_color"] == "#FFF6D5"
    assert ROW_STYLE_COLORS["DXF_ONLY"]["bg_color"] == "#FEF1F5"
    assert ROW_STYLE_COLORS["ULKES_ONLY"]["bg_color"] == "#EAF1FB"
