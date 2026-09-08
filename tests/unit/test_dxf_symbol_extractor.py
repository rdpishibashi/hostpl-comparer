"""model.dxf_symbol_extractor の単体テスト。

drawing_number_from_filename() と build_dxf_symbol_map() は合成データ（Counter・
文字列）だけで検証できるため純粋な単体テストとする。実際のDXF解析パイプライン
（ref_designator.extract_ref_designator_data() 経由）の検証は
tests/regression/spec/test_dxf_symbol_extractor_real_data.py（実DXFサンプル）で行う。
"""
from collections import Counter

from model.dxf_symbol_extractor import (
    build_dxf_symbol_map,
    drawing_number_from_filename,
)


def test_drawing_number_from_filename_strips_extension():
    assert drawing_number_from_filename("EE6312-000-01A.dxf") == "EE6312-000-01A"


def test_drawing_number_from_filename_uses_basename_only():
    assert drawing_number_from_filename("/tmp/some/path/EE6312-000-01A.dxf") == "EE6312-000-01A"


def _result(drawing_number, filename, labels, unconfirmed=(), warning=None):
    return {
        'drawing_number': drawing_number,
        'filename': filename,
        'counter': Counter(labels),
        'unconfirmed_labels': set(unconfirmed),
        'warning': warning,
    }


def test_build_dxf_symbol_map_single_file_per_drawing():
    results = [
        _result("EE0001-000-01A", "EE0001-000-01A.dxf", ["R1", "R1", "CN1"], unconfirmed=["CN1"]),
    ]
    symbol_map, unconfirmed_map, warnings = build_dxf_symbol_map(results)

    assert symbol_map == {"EE0001-000-01A": Counter({"R1": 2, "CN1": 1})}
    assert unconfirmed_map == {"EE0001-000-01A": {"CN1"}}
    assert warnings == []


def test_build_dxf_symbol_map_merges_duplicate_drawing_numbers_and_warns():
    results = [
        _result("EE0001-000-01A", "a.dxf", ["R1"]),
        _result("EE0001-000-01A", "b.dxf", ["R1", "C1"]),
    ]
    symbol_map, unconfirmed_map, warnings = build_dxf_symbol_map(results)

    assert symbol_map == {"EE0001-000-01A": Counter({"R1": 2, "C1": 1})}
    assert len(warnings) == 1
    assert "EE0001-000-01A" in warnings[0]
    assert "b.dxf" in warnings[0]


def test_build_dxf_symbol_map_collects_frame_detection_warning():
    results = [
        _result("EE0001-000-01A", "a.dxf", ["R1"], warning="図面枠が見つかりません"),
    ]
    _symbol_map, _unconfirmed_map, warnings = build_dxf_symbol_map(results)

    assert len(warnings) == 1
    assert "a.dxf" in warnings[0]
    assert "図面枠が見つかりません" in warnings[0]
