"""model.extract_symbols の単体テスト（合成データ）。"""
import pandas as pd
import pytest

from model.extract_symbols import extract_all_assemblies, extract_circuit_symbols


def _df(rows):
    """テスト用DataFrameを組み立てる。rowsは(図面番号, 符号, 構成コメント, 構成数)のタプル列。"""
    return pd.DataFrame(
        rows, columns=["図面番号", "符号", "構成コメント", "構成数"]
    )


def test_extract_all_assemblies_detects_multiple_assemblies():
    """複数アセンブリが展開されたPLファイルから全アセンブリを自動検出する。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "R1", None, 1),
        (None, "C1_C2", None, 2),
        ("EE0002-000-01A", None, None, None),
        (None, "CN1", None, 1),
    ])

    result, no_expansion, warnings = extract_all_assemblies(df)

    assert result == {
        "EE0001-000-01A": ["R1", "C1", "C2"],
        "EE0002-000-01A": ["CN1"],
    }
    assert no_expansion == []
    assert warnings == []


def test_extract_all_assemblies_reports_no_expansion_blocks():
    """部品展開が0行のアセンブリ（図面参照行のみ）を別リストで報告する。"""
    df = _df([
        ("EE0001-000-01A", "001", None, 1),
        ("EE0002-000-01A", None, None, None),
        (None, "R1", None, 1),
    ])

    result, no_expansion, warnings = extract_all_assemblies(df)

    assert result == {"EE0002-000-01A": ["R1"]}
    assert no_expansion == ["EE0001-000-01A"]
    assert warnings == []


def test_extract_all_assemblies_warns_on_duplicate_assembly_number_in_file():
    """同一アセンブリ番号がファイル内に複数回出現する場合、最初のブロックのみ採用し警告する。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "R1", None, 1),
        ("EE0001-000-01A", None, None, None),
        (None, "C1", None, 1),
    ])

    result, no_expansion, warnings = extract_all_assemblies(df)

    assert result == {"EE0001-000-01A": ["R1"]}
    assert no_expansion == []
    assert len(warnings) == 1
    assert "EE0001-000-01A" in warnings[0]


def test_extract_all_assemblies_raises_on_missing_required_columns():
    df = pd.DataFrame({"図面番号": ["EE0001-000-01A"]})
    with pytest.raises(ValueError):
        extract_all_assemblies(df)


def test_extract_circuit_symbols_unaffected_by_other_blocks_in_file():
    """extract_circuit_symbols()は他のブロックの有無に影響されない（単一アセンブリ抽出の従来動作）。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "R1", None, 1),
        ("EE0002-000-01A", None, None, None),
        (None, "CN1", None, 1),
    ])

    symbols, row_count = extract_circuit_symbols(df, "EE0002-000-01A")

    assert symbols == ["CN1"]
    assert row_count == 1


def test_extract_circuit_symbols_returns_empty_when_assembly_not_found():
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "R1", None, 1),
    ])

    symbols, row_count = extract_circuit_symbols(df, "EE9999-000-01A")

    assert symbols == []
    assert row_count == 0


def test_shortfall_and_excess_completion():
    """構成数との過不足補完ロジック。不足分は機器符号自身の名前で補完し
    （2026-09-09、ユーザー指定）、超過分は従来通り末尾に?マークを付ける。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "CNCB001W", None, 3),   # 不足: 1個→3個(CNCB001W自身の名前で2個補完)
        (None, "R1_R2_R3", None, 2),   # 超過: 3個→2個
    ])

    symbols, row_count = extract_circuit_symbols(df, "EE0001-000-01A")

    assert symbols == ["CNCB001W", "CNCB001W?001", "CNCB001W?002", "R1", "R2?"]
    assert row_count == 2


# --- 符号/構成コメントの個数比較による採用（2026-09-08、ユーザー指定の3例） ---

def test_field_selection_both_agree_prefers_comment():
    """符号=範囲表記(8個)・構成コメント=アンダースコア区切り(8個)で同数 → 構成コメントを採用。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "CNFAN01-08", "CNFAN01_CNFAN02_CNFAN03_CNFAN04_CNFAN05_CNFAN06_CNFAN07_CNFAN08", 8),
    ])

    symbols, _row_count = extract_circuit_symbols(df, "EE0001-000-01A")

    assert symbols == [f"CNFAN0{i}" for i in range(1, 9)]


def test_field_selection_comment_has_fewer_items_uses_symbol_range():
    """構成コメントが1個不足(7個) → 符号の範囲表記(8個)を採用する。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "CNFAN01-08", "CNFAN01_CNFAN02_CNFAN03_CNFAN04_CNFAN05_CNFAN06_CNFAN07", 8),
    ])

    symbols, _row_count = extract_circuit_symbols(df, "EE0001-000-01A")

    assert symbols == [f"CNFAN0{i}" for i in range(1, 9)]


def test_field_selection_symbol_range_has_fewer_items_uses_comment():
    """符号の範囲表記が1個不足(7個) → 構成コメント(8個)を採用する。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "CNFAN01-07", "CNFAN01_CNFAN02_CNFAN03_CNFAN04_CNFAN05_CNFAN06_CNFAN07_CNFAN08", 8),
    ])

    symbols, _row_count = extract_circuit_symbols(df, "EE0001-000-01A")

    assert symbols == [f"CNFAN0{i}" for i in range(1, 9)]


def test_field_selection_free_text_comment_is_not_treated_as_symbol_list():
    """構成コメントが「_」を含まない自由記述メモの場合、機器符号として採用しない
    （符号にフォールバックする）。実データのELB001行で確認した回帰。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "ELB001", "2接点タイプ。UPS遮断用に使用する。", 1),
    ])

    symbols, _row_count = extract_circuit_symbols(df, "EE0001-000-01A")

    assert symbols == ["ELB001"]


def test_field_selection_symbol_range_with_invalid_order_is_not_expanded():
    """符号の範囲表記で開始>終了の場合は範囲展開せず単一の値として扱う。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "CNFAN08-01", None, 1),
    ])

    symbols, _row_count = extract_circuit_symbols(df, "EE0001-000-01A")

    assert symbols == ["CNFAN08-01"]


# --- 符号+構成コメントが完全に同じ行の合算（2026-09-09、ユーザー指定） ---

def test_duplicate_rows_are_merged_and_quantities_summed():
    """符号・構成コメントが両方とも完全に同じ行は1行として扱い、構成数を合算する。
    実データのCNCB001W×4行（構成数1,1,3,2）で確認した回帰。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "CNCB001W", None, 1),
        (None, "CNCB001W", None, 1),
        (None, "CNCB001W", None, 3),
        (None, "CNCB001W", None, 2),
    ])

    symbols, row_count = extract_circuit_symbols(df, "EE0001-000-01A")

    # 合算後の構成数は7、機器符号は1個(CNCB001W)なので不足6個をCNCB001W自身の
    # 名前で補完する（2026-09-09、ユーザー指定）
    assert symbols == [
        "CNCB001W", "CNCB001W?001", "CNCB001W?002", "CNCB001W?003",
        "CNCB001W?004", "CNCB001W?005", "CNCB001W?006",
    ]
    assert row_count == 4  # 対象行数は生の行数のまま(合算してもここは変えない)


def test_rows_with_same_symbol_but_different_comment_are_not_merged():
    """符号が同じでも構成コメントが異なれば別行のまま扱う。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "CNCB001W", "CNCB001W_CNCB002W", 2),
        (None, "CNCB001W", None, 1),
    ])

    symbols, row_count = extract_circuit_symbols(df, "EE0001-000-01A")

    assert symbols == ["CNCB001W", "CNCB002W", "CNCB001W"]
    assert row_count == 2


def test_duplicate_rows_preserve_first_occurrence_order():
    """合算後のグループは最初に出現した順序を維持する。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "R1", None, 1),
        (None, "C1", None, 1),
        (None, "R1", None, 1),  # R1の2件目(合算対象)。出現順はR1が先のまま
    ])

    symbols, row_count = extract_circuit_symbols(df, "EE0001-000-01A")

    # R1は構成数合算2だが機器符号1個→不足1個をR1自身の名前で補完。C1はそのまま
    assert symbols == ["R1", "R1?001", "C1"]
    assert row_count == 3
