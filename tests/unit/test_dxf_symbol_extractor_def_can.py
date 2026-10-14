"""このテストが守るもの: DXF側の符号は「機器符号候補」列が DEF または CAN のラベルだけ。

DEF = 機器符号一覧（master_index）にあるラベル、CAN = 候補パターンに一致するラベル
（DXF-extract-labels と同じ `ref_designator.designator_mark()`）。どちらでもないラベルは
`rejected_labels` に回り、ULKES側の符号と一致する場合のみ従来どおり救済される。
"""
from model import dxf_symbol_extractor as ex


class _Index:
    def __init__(self, labels):
        self._labels = set(labels)

    def contains(self, text):
        return text in self._labels


def _run(monkeypatch, labels, master_index):
    monkeypatch.setattr(ex.ref_designator, 'collect_in_frame_labels',
                        lambda path: {'error': None, 'labels': [(l, 0, 0) for l in labels]})
    return ex.extract_symbols_from_dxf_file('x.dxf', 'EE0000-000-00A.dxf', master_index=master_index)


def test_can_labels_adopted_and_non_designator_rejected(monkeypatch):
    r = _run(monkeypatch, ['CP004', 'GND', 'CP004'], None)
    assert dict(r['counter']) == {'CP004': 2}
    assert dict(r['rejected_labels']) == {'GND': 1}


def test_def_label_adopted_even_if_not_can(monkeypatch):
    r = _run(monkeypatch, ['GND', 'CP004'], _Index({'GND'}))
    assert set(r['counter']) == {'GND', 'CP004'}
    assert not r['rejected_labels']
