"""実DXF（EE6676-601-02A）で、領域検出→「特定」「除外」→機器符号抽出が通ることを確認する。

受入条件: 「特定」領域を指定すると抽出ラベルがその内側だけの部分集合になり（scope=特定領域）、
「除外」だけ指定すると図面全体から除外領域内を除いた集合になる（scope=図面全体）。
region_filter なし（従来）の結果は変わらない。
"""
import os

import pytest

from model.dxf_symbol_extractor import (
    SCOPE_REGIONS, SCOPE_WHOLE, extract_symbols_from_dxf_file,
)
from model.extraction_pipeline import detect_file_regions
from view import region_selection
from config import region_detection_config

PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'sample_data', 'DXF_files',
                    'EE6676-601-02A.dxf')
NAME = 'EE6676-601-02A.dxf'


@pytest.fixture(scope='module')
def analysis():
    if not os.path.exists(PATH):
        pytest.skip(f'サンプルDXFが見つかりません: {PATH}')
    return detect_file_regions(
        PATH, NAME, region_selection.build_region_cfg(region_detection_config))


def _extract(analysis, selected=(), excluded=()):
    name_selections = {(NAME, r['id']): [r['name_candidates'][0][1]]
                       for r in analysis['regions']
                       if r['name_candidates'] and r['name_candidates'][0][1] in selected}
    return extract_symbols_from_dxf_file(PATH, NAME, region_filter={
        'analysis': analysis, 'name_selections': name_selections,
        'excluded_names': set(excluded), 'fname': NAME})


def test_selected_region_is_subset_of_whole(analysis):
    whole = extract_symbols_from_dxf_file(PATH, NAME)
    assert whole['scope'] is None
    sub = _extract(analysis, selected=['SYSTEM I/F BOX'])
    assert sub['scope'] == SCOPE_REGIONS and sub['scope_regions'] == ['SYSTEM I/F BOX']
    assert 0 < sum(sub['counter'].values()) < sum(whole['counter'].values())
    assert set(sub['counter']) <= set(whole['counter'])


def test_excluded_region_removes_labels_from_whole(analysis):
    whole = extract_symbols_from_dxf_file(PATH, NAME)
    out = _extract(analysis, excluded=['SYSTEM I/F BOX'])
    assert out['scope'] == SCOPE_WHOLE
    assert sum(out['counter'].values()) < sum(whole['counter'].values())
