"""
このテストが守るもの: 唯一のタイトルブロック（図面枠を含むINSERT）が
off/frozenレイヤーに置かれている図面でも、`extract_labels()`（図番）・
`ref_designator.collect_in_frame_labels()`・`region_detector.analyze_dxf_regions()`
（いずれも図面枠）が正しく検出できること。ただし出力ラベル自体は常に表示中の
エンティティのみに限定される（非表示タイトルブロックの文字は出力に混入しない）
こと。

不具合の識別子: 2026-09-23 ユーザー報告（DXF-extract-labelsで発覚）
    2026-09-16のレイヤー単位off/frozen判定追加（
    `test_off_frozen_layer_titleblock_excluded.py` が固定）により、
    唯一のタイトルブロックがoff/frozenレイヤーに置かれている図面
    （実データ EE5322-455-02A.dxf/EE5322-455-18A.dxf）で、図番・図面枠の
    手がかりが一切残らなくなっていた。

以前どう壊れていたか:
    is_invisible() がレイヤー単位でエンティティを除外する際、除外対象が
    「他に候補がある中の余剰（旧タイトルブロック等）」であることを前提と
    しており、「唯一の候補・唯一の図面枠がたまたま非表示レイヤーに
    置かれている」ケースを考慮していなかった。

修正後に保証したいこと:
    - 表示中のエンティティだけでは図番・図面枠の手がかりが1件も見つから
      ない場合に限り、レイヤーoff/frozenを無視した（entity自身のinvisible
      属性のみの）フォールバック探索で発見する。
    - 出力ラベルは、フォールバック発動時でも常に表示中のエンティティのみを
      対象にする——非表示タイトルブロックの文字はフォールバック発動時も
      出力ラベルに一切混入しない。
    - 表示中のタイトルブロックが既にある通常の図面では、フォールバックは
      発動せず従来通りの結果になる。

実行:
    cd HostPL-comparer
    python -m pytest tests/regression/bugfix/test_hidden_only_titleblock_frame_fallback.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import ezdxf

from model.extract_labels import extract_labels
from model import ref_designator
from model.region_detector import analyze_dxf_regions


def _save(doc):
    with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
        path = f.name
    doc.saveas(path)
    return path


def _build_doc_with_only_hidden_titleblock():
    """唯一のタイトルブロック（図面枠4辺＋図番'EE9999-001-01A'を持つINSERT）が
    off+frozenレイヤーに置かれている図面。タイトルブロック外に可視ラベル
    'R10' を1つ配置する。"""
    doc = ezdxf.new()
    msp = doc.modelspace()

    block = doc.blocks.new(name='TITLEBLOCK')
    for p1, p2 in [((0, 0), (200, 0)), ((200, 0), (200, 100)),
                   ((200, 100), (0, 100)), ((0, 100), (0, 0))]:
        block.add_line(p1, p2, dxfattribs={'lineweight': 100, 'color': 7})
    block.add_text('EE9999-001-01A', dxfattribs={'insert': (170, 25)})
    block.add_text('TITLE', dxfattribs={'insert': (100, 50)})

    hidden_layer_name = 'OLD_TITLEBLOCK_LAYER'
    doc.layers.add(hidden_layer_name)
    hidden_layer = doc.layers.get(hidden_layer_name)
    hidden_layer.off()
    hidden_layer.freeze()

    msp.add_blockref('TITLEBLOCK', (0, 0), dxfattribs={'layer': hidden_layer_name})
    msp.add_text('R10', dxfattribs={'insert': (10, 10)})
    return doc


def test_extract_labels_recovers_drawing_number_via_fallback():
    path = _save(_build_doc_with_only_hidden_titleblock())
    try:
        labels, info = extract_labels(
            path, extract_drawing_numbers_option=True, original_filename='hidden_tb.dxf')
    finally:
        os.remove(path)
    assert info['main_drawing_number'] == 'EE9999-001-01A'
    assert 'R10' in labels
    assert 'EE9999-001-01A' not in labels
    assert 'TITLE' not in labels


def test_collect_in_frame_labels_detects_frame_via_fallback():
    path = _save(_build_doc_with_only_hidden_titleblock())
    try:
        result = ref_designator.collect_in_frame_labels(path, frame_lineweight=100)
    finally:
        os.remove(path)
    assert result['error'] is None
    assert len(result['frames']) == 1
    texts = {t for (t, _x, _y) in result['labels']}
    assert 'R10' in texts
    assert 'TITLE' not in texts
    assert 'EE9999-001-01A' not in texts


def test_analyze_dxf_regions_detects_frame_via_fallback():
    path = _save(_build_doc_with_only_hidden_titleblock())
    try:
        result = analyze_dxf_regions(path)
    finally:
        os.remove(path)
    assert result['error'] is None
    assert len(result['frames']) == 1
