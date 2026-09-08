"""extract_circuit_symbols() のリファクタ前後の同一性を保証する。

HostPL-extractor の model/extract_symbols.py から複製した本モジュールは、
_find_assembly_blocks() / _process_rows() への分解と extract_all_assemblies()
の追加を行っている。既存の公開関数 extract_circuit_symbols() の戻り値は
一切変わっていないことを、HostPL-extractor の TECHNICAL.md に記録された
実測値（対象行数・抽出記号数）でも確認する。
"""
import os

import pandas as pd
import pytest

from model.extract_symbols import extract_circuit_symbols

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "sample_data")

# (ファイル名, アセンブリ番号, 期待する対象行数, 期待する抽出記号数)
# HostPL-extractor/TECHNICAL.md「実データでの確認結果」節の実測値
CASES = [
    ("EE6312-000-01A.xlsx", "EE6312-000-01A", 26, 48),
    ("EE6313-000-01C.xlsx", "EE6313-000-01C", 26, 48),
    ("EE6661-000-05A.xlsx", "EE6661-000-05A", 17, 121),
]


@pytest.mark.parametrize("filename, assembly_number, expected_rows, expected_count", CASES)
def test_extract_circuit_symbols_matches_known_counts(
    filename, assembly_number, expected_rows, expected_count
):
    path = os.path.join(SAMPLE_DIR, filename)
    if not os.path.exists(path):
        pytest.skip(f"サンプルデータが見つかりません: {path}")

    df = pd.read_excel(path)
    symbols, row_count = extract_circuit_symbols(df, assembly_number)

    assert row_count == expected_rows
    assert len(symbols) == expected_count
