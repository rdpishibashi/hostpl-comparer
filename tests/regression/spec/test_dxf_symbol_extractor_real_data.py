"""実DXFサンプルによる model.dxf_symbol_extractor の回帰テスト。

ref_designator の各公開関数（collect_in_frame_labels/normalize_labels/
is_ref_designator_candidate）を経由した実際のDXF解析（図面枠検出→
フォーマットブロック除外→NFKC正規化→候補判定）は合成データでは再現できない
ため、実ファイルの構造そのものを使う。

期待値は2026-09-08、sample_data/DXF_files/*.dxf に対して実行して確認した実測値
（DXF-extract-labelsのアップデートで変化しうるため、値が変わったら
意図した変更か確認してから更新すること）。2026-09-09、`Tools/sample-dxf/`から
実DXFの図番とULKES PLの図番が一致する12件を追加（`problems/`・
`terminal-detector/`由来）。2026-09-11、invisible属性（非表示設定）付き
エンティティを収集対象から除外する修正に伴い、EE6892-612-01B.dxf・
EE6892-617-01B.dxfの2件で期待値を更新（いずれも旧版タイトルブロック
〈非表示設定で残存していた設計者名'Takahashi'/'TAKEDA'等〉がrejected_labels
から消えたことによる、意図した減少）。
"""
import glob
import os

import pytest

from model.dxf_symbol_extractor import extract_symbols_from_dxf_file

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "sample_data", "DXF_files")

# (ファイル名, 候補ラベル種類数, 候補総出現数, 非候補ラベル種類数, 非候補総出現数,
#  図面枠検出フォールバック警告の有無)
CASES = [
    ("EE6312-000-01A.dxf", 45, 66, 97, 297, False),
    ("EE6313-000-01C.dxf", 45, 66, 98, 286, False),
    ("EE6661-000-05A.dxf", 43, 58, 106, 431, False),
    ("EE2685-335-01D.dxf", 0, 0, 3, 3, False),
    ("EE2685-475-96A.dxf", 0, 0, 3, 3, False),
    ("EE5322-455-01B.dxf", 6, 10, 54, 74, True),
    ("EE5322-455-07A.dxf", 1, 1, 22, 24, True),
    ("EE6492-039-90A.dxf", 2, 2, 47, 67, False),
    ("EE6676-601-02A.dxf", 137, 159, 396, 956, False),
    ("EE6888-637-01A.dxf", 16, 16, 102, 144, False),
    ("EE6888-639-01A.dxf", 7, 7, 63, 76, False),
    ("EE6888-650-01C.dxf", 101, 128, 221, 509, False),
    ("EE6888-660-01A.dxf", 2, 2, 57, 64, False),
    ("EE6892-612-01B.dxf", 98, 100, 232, 507, False),
    ("EE6892-617-01B.dxf", 29, 31, 155, 258, False),
]


@pytest.mark.parametrize(
    "filename, expected_labels, expected_total, expected_rejected_labels, "
    "expected_rejected_total, expects_warning",
    CASES,
)
def test_extract_symbols_from_dxf_file_matches_known_counts(
    filename, expected_labels, expected_total, expected_rejected_labels,
    expected_rejected_total, expects_warning,
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
    if expects_warning:
        assert result['warning'] is not None
    else:
        assert result['warning'] is None


def test_all_sample_dxf_files_are_covered_by_cases():
    """sample_data配下の全DXFがCASESでカバーされていることを確認する
    （新しいサンプルを追加したのにケースを足し忘れる事故を防ぐ）。"""
    actual_files = {os.path.basename(f) for f in glob.glob(os.path.join(SAMPLE_DIR, "*.dxf"))}
    if not actual_files:
        pytest.skip("sample_data にDXFファイルがありません")
    expected_files = {c[0] for c in CASES}
    assert actual_files == expected_files
