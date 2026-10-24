"""
このテストが守るもの: HostPL-comparer の model/dxf_preview.py と fonts/ が、正本である
DXF-diff-manager の同名ファイルとバイト一致であること。

背景（2026-10-24）:
    図面プレビュー（render_drawing_svg + build_viewer_html(layer_panel=False)）は
    DXF-diff-manager の model/dxf_preview.py を正本とし、DXF-visual-diff と本プロジェクトはコピー。
    変更は DXF-diff-manager で行い、次のコマンドで伝播する:
        command cp -f model/dxf_preview.py ../HostPL-comparer/model/dxf_preview.py
    DXF-diff-manager が隣にない環境（Streamlit Cloud・単独クローン）では skip する。
    こちら側のファイルが無い場合は skip せず fail する。
"""
import hashlib
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_PRIMARY_ROOT = os.path.join(_ROOT, "..", "DXF-diff-manager")

_FILES = [
    os.path.join("model", "dxf_preview.py"),
    os.path.join("fonts", "ipaexg.ttf"),
    os.path.join("fonts", "IPA_Font_License_Agreement_v1.0.txt"),
    os.path.join("fonts", "Readme_IPAexfont00401.txt"),
]


def _md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


@pytest.mark.parametrize("rel_path", _FILES)
def test_preview_files_are_byte_identical_to_diff_manager(rel_path):
    mine = os.path.join(_ROOT, rel_path)
    assert os.path.exists(mine), f"HostPL-comparer 側にファイルがありません: {mine}"
    primary = os.path.join(_PRIMARY_ROOT, rel_path)
    if not os.path.exists(os.path.join(_PRIMARY_ROOT, "app.py")):
        pytest.skip(f"DXF-diff-manager が隣にチェックアウトされていないため比較できません ({_PRIMARY_ROOT})")
    assert os.path.exists(primary), f"DXF-diff-manager 側（正本）にファイルがありません: {primary}"
    assert _md5(mine) == _md5(primary), (
        f"{rel_path} が DXF-diff-manager（正本）と乖離しています。"
        "正本から command cp -f で再コピーしてください。")
