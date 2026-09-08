"""extract_circuit_symbols() の実データでの回帰テスト（総数の凍結）。

当初は_find_assembly_blocks()/_process_rows()への分解（ラウンド1）の前後で
挙動が変わっていないことを確認するテストだったが、その後のユーザー要求
（符号/構成コメントの個数比較選択、構成数超過補完を機器符号ごとに独立して
適用する方式への変更等）で総数自体が意図的に変わっている。現在はそれらの
変更を反映した実測値を凍結する回帰テストとして機能する。値が変わったら
意図した変更か確認してから更新すること。

2026-09-09の変更（構成数を機器符号ごとに独立して適用）で、「_」等で複数の
機器符号に分解される行の構成数が大きい場合、抽出数が大きく増える
（例: EE6661-000-05Aの"CNFAN01-08"×4行、構成数8+8+24+24=64が8個の機器符号
それぞれに独立して適用され、121→853に増加。ユーザー確認済み・意図した挙動）。
"""
import os

import pandas as pd
import pytest

from model.extract_symbols import extract_circuit_symbols

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "sample_data")

# (ファイル名, アセンブリ番号, 期待する対象行数, 期待する抽出記号数)
CASES = [
    ("EE6312-000-01A.xlsx", "EE6312-000-01A", 26, 62),
    ("EE6313-000-01C.xlsx", "EE6313-000-01C", 26, 62),
    ("EE6661-000-05A.xlsx", "EE6661-000-05A", 17, 853),
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
