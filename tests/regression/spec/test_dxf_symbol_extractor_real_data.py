"""実DXFサンプルによる model.dxf_symbol_extractor の回帰テスト。

ref_designator の各公開関数（collect_in_frame_labels/normalize_labels/
is_ref_designator_candidate）を経由した実際のDXF解析（図面枠検出→
フォーマットブロック除外→NFKC正規化→候補判定）は合成データでは再現できない
ため、実ファイルの構造そのものを使う。

期待値は2026-09-08、sample_data/*.dxf に対して実行して確認した実測値
（DXF-extract-labelsのアップデートで変化しうるため、値が変わったら
意図した変更か確認してから更新すること）。
"""
import glob
import os

import pytest

from model.dxf_symbol_extractor import extract_symbols_from_dxf_file

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "sample_data")

# (ファイル名, 候補ラベル種類数, 候補総出現数, 非候補ラベル種類数, 非候補総出現数)
CASES = [
    ("EE6312-000-01A.dxf", 45, 66, 97, 297),
    ("EE6313-000-01C.dxf", 45, 66, 98, 286),
    ("EE6661-000-05A.dxf", 43, 58, 106, 431),
]


@pytest.mark.parametrize(
    "filename, expected_labels, expected_total, expected_rejected_labels, expected_rejected_total",
    CASES,
)
def test_extract_symbols_from_dxf_file_matches_known_counts(
    filename, expected_labels, expected_total, expected_rejected_labels, expected_rejected_total
):
    path = os.path.join(SAMPLE_DIR, filename)
    if not os.path.exists(path):
        pytest.skip(f"サンプルDXFが見つかりません: {path}")

    result = extract_symbols_from_dxf_file(path, original_filename=filename)

    assert result['drawing_number'] == os.path.splitext(filename)[0]
    assert len(result['counter']) == expected_labels
    assert sum(result['counter'].values()) == expected_total
    assert len(result['rejected_labels']) == expected_rejected_labels
    assert sum(result['rejected_labels'].values()) == expected_rejected_total
    assert result['warning'] is None


def test_all_sample_dxf_files_are_covered_by_cases():
    """sample_data配下の全DXFがCASESでカバーされていることを確認する
    （新しいサンプルを追加したのにケースを足し忘れる事故を防ぐ）。"""
    actual_files = {os.path.basename(f) for f in glob.glob(os.path.join(SAMPLE_DIR, "*.dxf"))}
    if not actual_files:
        pytest.skip("sample_data にDXFファイルがありません")
    expected_files = {c[0] for c in CASES}
    assert actual_files == expected_files
