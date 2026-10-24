"""HostPL-comparer（config.py に diff_config が無い）で、図面プレビュー用の dxf_preview が使えること。"""
import ezdxf
import pytest

from model import dxf_preview


def _dxf_bytes():
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 50))
    msp.add_text("ABC", dxfattribs={"insert": (10, 10), "height": 5})
    import io
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


def test_viewer_without_layer_panel_has_only_fit_and_fullscreen():
    """守るもの: diff_config が無くても import でき、render_drawing_svg → build_viewer_html(layer_panel=False)
    が全体表示・全画面ボタンだけの viewer（レイヤー・プリセットの操作なし）を返す。"""
    result = dxf_preview.render_drawing_svg(_dxf_bytes())
    html_text = dxf_preview.build_viewer_html(result, layer_panel=False)
    assert "全体表示" in html_text and "全画面" in html_text
    # JS 本体は常に 'data-preset' を含むため、描画される要素（見出し・ボタン・チェックボックス）で確認する
    assert "<h4>表示プリセット</h4>" not in html_text and "<button data-preset" not in html_text
    assert "<input type=\"checkbox\" data-layer" not in html_text


def test_render_layers_svg_is_unavailable_without_diff_config():
    """守るもの: 差分DXF用の render_layers_svg は、レイヤー色（diff_config）が無いと明示的に失敗する
    （黙って空の結果を返さない）。"""
    if dxf_preview.PREVIEW_LAYERS:
        pytest.skip("diff_config があるプロジェクトでは対象外")
    with pytest.raises(RuntimeError):
        dxf_preview.render_layers_svg(_dxf_bytes())
