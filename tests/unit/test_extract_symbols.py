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


def test_shortfall_and_excess_completion_unchanged():
    """構成数との過不足補完ロジック（?ddd補完・末尾?マーク）が従来通り動作する。"""
    df = _df([
        ("EE0001-000-01A", None, None, None),
        (None, "CNCB001W", None, 3),   # 不足: 1個→3個
        (None, "R1_R2_R3", None, 2),   # 超過: 3個→2個
    ])

    symbols, row_count = extract_circuit_symbols(df, "EE0001-000-01A")

    assert symbols == ["CNCB001W", "CNCB?001", "CNCB?002", "R1", "R2?"]
    assert row_count == 2
