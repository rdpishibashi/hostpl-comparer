"""model.dxf_symbol_extractor の単体テスト。

drawing_number_from_filename() と build_dxf_symbol_map() は合成データ（Counter・
文字列）だけで検証できるため純粋な単体テストとする。実際のDXF解析パイプライン
（ref_designator の各公開関数経由）の検証は
tests/regression/spec/test_dxf_symbol_extractor_real_data.py（実DXFサンプル）で行う。
"""
from collections import Counter

from model import ref_designator
from model.dxf_symbol_extractor import (
    build_dxf_symbol_map,
    drawing_number_from_filename,
)


def test_ref_designator_private_fallback_function_still_exists():
    """`ref_designator._collect_all_labels_fallback()` は非公開関数（先頭
    アンダースコア）だが、dxf_symbol_extractor.py が直接呼び出している。
    primary側でリネーム・削除されると dxf_symbol_extractor.py が黙って壊れる
    ため、存在を確認するガードテストを置く。"""
    assert hasattr(ref_designator, "_collect_all_labels_fallback")
    assert callable(ref_designator._collect_all_labels_fallback)


def test_drawing_number_from_filename_strips_extension():
    assert drawing_number_from_filename("EE6312-000-01A.dxf") == "EE6312-000-01A"


def test_drawing_number_from_filename_uses_basename_only():
    assert drawing_number_from_filename("/tmp/some/path/EE6312-000-01A.dxf") == "EE6312-000-01A"


def _result(drawing_number, filename, labels, rejected=(), warning=None):
    return {
        'drawing_number': drawing_number,
        'filename': filename,
        'counter': Counter(labels),
        'rejected_labels': Counter(rejected),
        'warning': warning,
    }


def test_build_dxf_symbol_map_single_file_per_drawing():
    results = [
        _result("EE0001-000-01A", "EE0001-000-01A.dxf", ["R1", "R1", "CN1"], rejected=["GND"]),
    ]
    symbol_map, rejected_map, no_frame_filenames, warnings = build_dxf_symbol_map(results)

    assert symbol_map == {"EE0001-000-01A": Counter({"R1": 2, "CN1": 1})}
    assert rejected_map == {"EE0001-000-01A": Counter({"GND": 1})}
    assert no_frame_filenames == []
    assert warnings == []


def test_build_dxf_symbol_map_takes_first_file_on_duplicate_drawing_number_and_warns():
    """同一図番のDXFファイルが複数ある場合は合算せず、最初の1件を採用する（要求8）。"""
    results = [
        _result("EE0001-000-01A", "a.dxf", ["R1"], rejected=["X1"]),
        _result("EE0001-000-01A", "b.dxf", ["R1", "C1"], rejected=["X2"]),
    ]
    symbol_map, rejected_map, no_frame_filenames, warnings = build_dxf_symbol_map(results)

    assert symbol_map == {"EE0001-000-01A": Counter({"R1": 1})}
    assert rejected_map == {"EE0001-000-01A": Counter({"X1": 1})}
    assert no_frame_filenames == []
    assert len(warnings) == 1
    assert "EE0001-000-01A" in warnings[0]
    assert "b.dxf" in warnings[0]


def test_build_dxf_symbol_map_collects_no_frame_filename_as_raw_name():
    """図面枠検出フォールバックが発生したファイルは、メッセージ文字列ではなく
    生のファイル名のリスト（`no_frame_filenames`）として返す（2026-09-09、
    ユーザー指定。呼び出し元がまとめて「図面枠を検出できないDXFファイル」として
    表示する）。`warnings`にはこの件は含まれない（図番重複のみを扱う）。"""
    results = [
        _result("EE0001-000-01A", "a.dxf", ["R1"], warning="図面枠が見つかりません"),
    ]
    _symbol_map, _rejected_map, no_frame_filenames, warnings = build_dxf_symbol_map(results)

    assert no_frame_filenames == ["a.dxf"]
    assert warnings == []
