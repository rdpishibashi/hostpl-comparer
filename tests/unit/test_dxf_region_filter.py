"""model.dxf_symbol_extractor._apply_region_filter の単体テスト（合成データ）。

「指定領域で比較する」のDXF側絞り込み: 「除外」領域の内側を除き、「特定」領域が
あればその内側だけを残す。「特定」領域が無ければ図面全体（除外後）にフォールバックする。
"""
from model.dxf_symbol_extractor import SCOPE_REGIONS, SCOPE_WHOLE, _apply_region_filter

# 3つの領域（左から A=(0,0)-(10,10), B=(20,0)-(30,10), C=(40,0)-(50,10)）
def _poly(x0):
    return [(x0, 0), (x0 + 10, 0), (x0 + 10, 10), (x0, 10)]


ANALYSIS = {
    'regions': [
        {'id': 0, 'polygon': _poly(0), 'frame': 0, 'area_pct': 10.0,
         'name_candidates': [(0.0, 'AREA A')]},
        {'id': 1, 'polygon': _poly(20), 'frame': 0, 'area_pct': 10.0,
         'name_candidates': [(0.0, 'AREA B')]},
        {'id': 2, 'polygon': _poly(40), 'frame': 0, 'area_pct': 10.0,
         'name_candidates': [(0.0, 'AREA C')]},
    ],
}
LABELS = [('R001', 5, 5), ('C001', 25, 5), ('L001', 45, 5), ('Q001', 100, 5)]   # 最後は領域外


def _filter(selected=(), excluded=()):
    name_selections = {('f.dxf', i): [n] for i, n in enumerate(['AREA A', 'AREA B', 'AREA C'])
                       if n in selected}
    return _apply_region_filter(LABELS, {
        'analysis': ANALYSIS, 'name_selections': name_selections,
        'excluded_names': set(excluded), 'fname': 'f.dxf'})


def test_selected_regions_only_keeps_labels_inside_them():
    kept, scope, regions = _filter(selected=['AREA A', 'AREA C'])
    assert [t for t, _x, _y in kept] == ['R001', 'L001']
    assert scope == SCOPE_REGIONS and regions == ['AREA A', 'AREA C']


def test_no_selected_region_falls_back_to_whole_drawing():
    """「特定」領域が無い図面は、図面全体（領域の内外を問わない）を使う。"""
    kept, scope, regions = _filter()
    assert len(kept) == 4
    assert scope == SCOPE_WHOLE and regions == []


def test_excluded_region_removed_from_whole_drawing():
    kept, scope, _regions = _filter(excluded=['AREA B'])
    assert [t for t, _x, _y in kept] == ['R001', 'L001', 'Q001']
    assert scope == SCOPE_WHOLE


def test_excluded_removed_even_when_selected_regions_exist():
    """「特定」A・B と「除外」B: Bの内側は残らない（除外が優先）。"""
    kept, scope, _regions = _filter(selected=['AREA A', 'AREA B'], excluded=['AREA B'])
    assert [t for t, _x, _y in kept] == ['R001']
    assert scope == SCOPE_REGIONS


def test_selected_region_not_in_this_drawing_falls_back_for_this_file():
    """このファイルに該当する「特定」領域名が無ければ、このファイルだけ図面全体になる。"""
    kept, scope, _regions = _filter(selected=['NO SUCH AREA'])
    assert len(kept) == 4 and scope == SCOPE_WHOLE
