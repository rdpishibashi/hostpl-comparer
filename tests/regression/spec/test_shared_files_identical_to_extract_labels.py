"""
このテストが守るもの: DXF処理の共有ファイルが DXF-extract-labels（primary）とバイト一致で
あること。

受入条件（Given/When/Then）:
  Given HostPL-comparer と DXF-extract-labels が同じ親フォルダにチェックアウトされている
  When  下記 SHARED_FILES を比較する
  Then  全ファイルの MD5 が一致する

DXF-extract-labels が隣に無い環境（Streamlit Cloud・単独クローン）では skip する。
共有ファイルを変更するときは必ず DXF-extract-labels 側で行い、そのままコピーすること
（Tools/CLAUDE.md「Shared DXF Processing Library Pattern」）。
"""
import hashlib
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
PRIMARY = os.path.abspath(os.path.join(PROJECT, '..', 'DXF-extract-labels'))

SHARED_FILES = (
    'model/extract_labels.py',
    'model/common_utils.py',
    'model/ref_designator.py',
    'model/label_classifier.py',
    'model/region_detector.py',
    'model/extraction_pipeline.py',
    'view/region_selection.py',
    'config.py',
)


def _md5(path):
    with open(path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


@pytest.mark.parametrize('rel_path', SHARED_FILES)
def test_shared_file_identical_to_primary(rel_path):
    primary = os.path.join(PRIMARY, rel_path)
    if not os.path.isfile(primary):
        pytest.skip(f'DXF-extract-labels が隣にありません: {primary}')
    assert _md5(os.path.join(PROJECT, rel_path)) == _md5(primary), (
        f'{rel_path} が DXF-extract-labels とバイト一致しません。'
        'DXF-extract-labels 側から再コピーしてください。')
