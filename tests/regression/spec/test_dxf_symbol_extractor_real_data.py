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
から消えたことによる、意図した減少）。2026-09-16、is_invisible()へのレイヤー
単位（オフ/フリーズ）可視性チェック追加に伴い、EE6888-637-01A.dxf・
EE6888-639-01A.dxf・EE6892-617-01B.dxfの3件で期待値を更新。エンティティ自身の
invisible属性は立っていないが、専用レイヤーごとオフ+フリーズされた旧版
タイトルブロック一式（設計者名'TAKEDA'/'TAKAKI'、'TMP'という誤った機器符号
候補、旧図番EE3273-637-01A/02B・EE3273-639-01A/EE3792-639-01A・
EE3273-529-01A/EE3273-617-01A、DATE/DESIG/DRAW/MARK/MFG No./NAME/REMARKS/
REVISION/SCALE/TITLE/TOLERANCES等のタイトルブロック項目名、承認/検図/設計/
製図等の日本語承認欄）が、いずれのファイルも実データでレイヤーのoff+frozenを
確認したうえで除外されたことによる、意図した減少（新規に増えたラベルは無し）。
2026-09-23、`ref_designator._collect_all_labels_fallback()`（図面枠フォール
バック経路、本ファイルでは`EE5322-455-01B.dxf`/`EE5322-455-07A.dxf`が該当）に
初めて`is_invisible()`チェックを追加したことに伴い、この2件で期待値を更新。
減少分（`Ｍ１`・`Ｍ２`・`Ｍ１－Ｍ１`・`Ｍ２－Ｍ２`・`Ｊ`・`Ｊ－Ｊ`等の
セクションビュー記号）はentity自身のinvisible属性が立っていることを実データで
確認したうえでの、意図した減少（ユーザー確認済み）。
2026-10-14、DXF-extract-labelsの`OTHER_PATTERNS`（A・C・L・Q・U・F以外の英大文字
1字+数字1〜2桁。`R10`・`T1`・`N24`等）を除外カテゴリ`single_letter_digits_other`
として移植したことに伴い、該当ラベルを含む8件で期待値を更新（候補→非候補への
意図した移動。候補種類数の減少分だけ非候補が増える。非候補の種類数が減少分より
少ないのは、元から非候補にも同名ラベルがあるため）。
"""
import glob
import os

import pytest

from model.dxf_symbol_extractor import extract_symbols_from_dxf_file

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "sample_data", "DXF_files")

# (ファイル名, 候補ラベル種類数, 候補総出現数, 非候補ラベル種類数, 非候補総出現数,
#  図面枠検出フォールバック警告の有無)
CASES = [
    ("EE6312-000-01A.dxf", 40, 55, 102, 308, False),
    ("EE6313-000-01C.dxf", 40, 55, 103, 297, False),
    ("EE6661-000-05A.dxf", 43, 58, 106, 431, False),
    ("EE2685-335-01D.dxf", 0, 0, 3, 3, False),
    ("EE2685-475-96A.dxf", 0, 0, 3, 3, False),
    ("EE5322-455-01B.dxf", 1, 1, 53, 74, True),
    ("EE5322-455-07A.dxf", 1, 1, 18, 18, True),
    ("EE6492-039-90A.dxf", 0, 0, 49, 69, False),
    ("EE6676-601-02A.dxf", 94, 111, 439, 1004, False),
    ("EE6888-637-01A.dxf", 15, 15, 67, 103, False),
    ("EE6888-639-01A.dxf", 5, 5, 34, 39, False),
    ("EE6888-650-01C.dxf", 80, 101, 242, 536, False),
    ("EE6888-660-01A.dxf", 2, 2, 57, 64, False),
    ("EE6892-612-01B.dxf", 72, 74, 258, 533, False),
    ("EE6892-617-01B.dxf", 22, 23, 128, 226, False),
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
