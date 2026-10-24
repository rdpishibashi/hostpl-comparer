"""
差分DXFのブラウザ内プレビュー（モデル層。streamlit 非依存）

差分DXF（compare_dxf.py の出力）の5つのカテゴリ別レイヤーを ezdxf で SVG に変換し、
自己完結した HTML（inline SVG + 素の JS による pan/zoom・レイヤー ON/OFF）として返す。

設計上の要点:
- OLD_ALL/NEW_ALL は他レイヤーの物理複製なので描画しない（サイズ倍増を避ける）。
  「NEW 全体」「OLD 全体」はブラウザ側のプリセットで表現する
- 各レイヤーを共通の render_box で個別に SVG 化し、座標系を揃えて重ねる
- SVG のクラス名（.C1 等）はレイヤー間で衝突するため、レイヤー固有の接頭辞を付ける
- 差分DXFは5カテゴリ層が既定で OFF のため、レイヤー表示は RenderContext の
  レイヤープロパティ上書きで制御する
- 外部 JS・CDN には依存しない（社内ネットワークで遮断される可能性があるため）
- 他図面の表示（`render_drawing_svg()`）は、アップロードされた DXF を元の色のまま1枚の SVG にする
  （レイヤー上書きなし＝DXF 上で非表示の画層は描かない）。左パネルを操作だけにした viewer は
  `build_viewer_html(layer_panel=False)`
- `config.diff_config` が無いプロジェクトでは `render_drawing_svg()` と `layer_panel=False` の viewer だけを使える
"""

import html
import io
from pathlib import Path
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import ezdxf
from ezdxf import colors as ezdxf_colors
from ezdxf import recover
from ezdxf.addons.drawing import Frontend, RenderContext, config as drawing_config, layout, svg
from ezdxf.fonts import font_manager as ezdxf_font_manager
from ezdxf.fonts import fonts as ezdxf_fonts
from ezdxf.math import BoundingBox2d

try:
    from config import diff_config
except ImportError:
    # config.py に diff_config が無いプロジェクト（HostPL-comparer。config.py は DXF-extract-labels と共有）は
    # 差分DXFを描かない（render_drawing_svg() と build_viewer_html(layer_panel=False) だけを使う）
    diff_config = None

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

# プレビュー対象のカテゴリ別レイヤー: (レイヤー名, 表示名, 既定色(ACI))
PREVIEW_LAYERS: List[Tuple[str, str, int]] = [] if diff_config is None else [
    ("NEW_ADDED", "追加（NEWのみ）", diff_config.DEFAULT_ADDED_COLOR),
    ("OLD_DELETED", "削除（OLDのみ）", diff_config.DEFAULT_DELETED_COLOR),
    ("UNCHANGED", "変更なし", diff_config.DEFAULT_UNCHANGED_COLOR),
    ("NEW_UNCHANGED_OFFSET", "オフセット一致（NEW座標）", diff_config.DEFAULT_UNCHANGED_OFFSET_NEW_COLOR),
    ("OLD_UNCHANGED_OFFSET", "オフセット一致（OLD座標）", diff_config.DEFAULT_UNCHANGED_OFFSET_OLD_COLOR),
]

# ブラウザ側プリセット: 表示名 -> 対象レイヤー（None は「すべて」）
PRESETS: List[Tuple[str, Optional[List[str]]]] = [
    ("NEW 全体", ["NEW_ADDED", "UNCHANGED", "NEW_UNCHANGED_OFFSET"]),
    ("OLD 全体", ["OLD_DELETED", "UNCHANGED", "OLD_UNCHANGED_OFFSET"]),
    ("差分のみ", ["NEW_ADDED", "OLD_DELETED"]),
    ("すべて", None),
]

# リポジトリ同梱フォント（IPAex ゴシック。IPA フォントライセンス v1.0、無改変で再配布）。
# Streamlit Cloud にはシステムフォントが1つも無く、apt（packages.txt）でも入らなかったため、
# 同梱して ezdxf のフォント管理に直接登録する
BUNDLED_FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
BUNDLED_FONT_NAME = "ipaexg.ttf"

# 日本語を描画できるフォントの探索順（先に見つかったものを使う）
CJK_FONT_CANDIDATES = [
    BUNDLED_FONT_NAME,  # 同梱の IPAex ゴシック（どの環境でも同じ見た目になるよう最優先）
    "ipag.ttf",         # IPA ゴシック
    "Arial Unicode.ttf",  # macOS
    "ArialUni.ttf",     # Windows
    "msgothic.ttc",     # Windows
    "meiryo.ttc",       # Windows
    "NotoSansCJK-Regular.ttc",
    "NotoSansCJKjp-Regular.otf",
]


@dataclass
class LayerSvg:
    name: str
    label: str
    color_hex: str
    count: int
    inner_svg: str  # <defs> の style と <g> を含む断片（クラス名はレイヤー固有の接頭辞付き）


@dataclass
class PreviewResult:
    layers: List[LayerSvg] = field(default_factory=list)
    view_box: Tuple[int, int] = (0, 0)
    cjk_font: Optional[str] = None
    empty: bool = False


def _register_bundled_font() -> None:
    """同梱フォントを ezdxf のフォント管理に登録する（未登録のときだけ）。

    フォントが1つも無い環境（Streamlit Cloud）では、文字の描画時に ezdxf が
    FontNotFoundError（"no fonts available, not even fallback fonts"）を出す。
    ezdxf は「見つからなかったフォントの代替」を最初に引いた時点で固定して
    キャッシュするため、登録後はその固定値も消して同梱フォントを代替にする。
    """
    manager = ezdxf_fonts.font_manager
    if not (BUNDLED_FONT_DIR / BUNDLED_FONT_NAME).is_file():
        return
    if not manager.has_font(BUNDLED_FONT_NAME):
        manager.build([str(BUNDLED_FONT_DIR)], support_dirs=False)
    if BUNDLED_FONT_NAME not in ezdxf_font_manager.DEFAULT_FONTS:
        ezdxf_font_manager.DEFAULT_FONTS.append(BUNDLED_FONT_NAME)
    manager._fallback_font_name = ""


def find_cjk_font() -> Optional[str]:
    """日本語を描画できるフォントのファイル名を返す。見つからなければ None。

    同梱フォントを先に登録してから `CJK_FONT_CANDIDATES` の順に探す。
    """
    _register_bundled_font()
    manager = ezdxf_fonts.font_manager
    for name in CJK_FONT_CANDIDATES:
        if manager.has_font(name):
            return name
    return None


def _aci_to_hex(aci: int) -> str:
    """ACI 色番号を CSS 色に変換する。7 は黒背景上で白に描かれるため白にする。"""
    if aci == 7:
        return "#ffffff"
    r, g, b = ezdxf_colors.aci2rgb(aci)
    return f"#{r:02x}{g:02x}{b:02x}"


def _read_doc(dxf_bytes: bytes):
    doc, _auditor = recover.read(io.BytesIO(dxf_bytes))
    return doc


def _apply_cjk_font(doc, font_name: str) -> None:
    """描画用に読み込んだ doc（コピー）の全テキストスタイルのフォントを差し替える。"""
    for style in doc.styles:
        style.dxf.font = font_name
        if style.dxf.hasattr("bigfont"):
            style.dxf.bigfont = ""


def _draw_layer(doc, layer_name: str) -> svg.SVGBackend:
    """指定レイヤーだけを表示状態にして SVGBackend に描画する。"""
    ctx = RenderContext(doc)

    def _override(layer_properties):
        for lp in layer_properties:
            lp.is_visible = lp.layer == layer_name

    ctx.set_layer_properties_override(_override)
    backend = svg.SVGBackend()
    cfg = drawing_config.Configuration(background_policy=drawing_config.BackgroundPolicy.BLACK)
    Frontend(ctx, backend, config=cfg).draw_layout(doc.modelspace())
    return backend


def _prefix_classes(root: ET.Element, prefix: str) -> Tuple[List[ET.Element], List[ET.Element]]:
    """SVG ルートから style 要素群と描画グループを取り出し、クラス名に接頭辞を付ける。"""
    styles: List[ET.Element] = []
    groups: List[ET.Element] = []
    for child in root:
        tag = child.tag.split("}")[-1]
        if tag == "defs":
            for style in child:
                style.text = (style.text or "").replace(".C", f".{prefix}C")
                styles.append(style)
        elif tag == "g":
            for el in child.iter():
                cls = el.get("class")
                if cls:
                    el.set("class", f"{prefix}{cls}")
            groups.append(child)
        # tag == "rect"（背景）は外側で一括して描くため捨てる
    return styles, groups


def render_layers_svg(dxf_bytes: bytes) -> PreviewResult:
    """差分DXFのバイト列からカテゴリ別レイヤーの SVG を生成する。

    元の dxf_bytes は変更しない（フォント差し替えは読み込んだ doc に対してのみ行う）。
    """
    if not PREVIEW_LAYERS:
        raise RuntimeError("差分DXFのレイヤー色（config.diff_config）がありません")
    doc = _read_doc(dxf_bytes)
    cjk_font = find_cjk_font()
    if cjk_font:
        _apply_cjk_font(doc, cjk_font)

    msp = doc.modelspace()
    counts = {name: 0 for name, _, _ in PREVIEW_LAYERS}
    for entity in msp:
        layer = entity.dxf.layer
        if layer in counts:
            counts[layer] += 1

    result = PreviewResult(cjk_font=cjk_font)
    if not any(counts.values()):
        result.empty = True
        for name, label, aci in PREVIEW_LAYERS:
            result.layers.append(LayerSvg(name, label, _aci_to_hex(aci), 0, ""))
        return result

    # パス1: 全レイヤーを描画して共通の描画範囲（和集合）を求める
    backends = {}
    render_box = BoundingBox2d()
    for name, _label, _aci in PREVIEW_LAYERS:
        if counts[name] == 0:
            continue
        backend = _draw_layer(doc, name)
        backends[name] = backend
        bbox = backend.player().bbox()
        if bbox.has_data:
            render_box = render_box.union(bbox)

    if not render_box.has_data:
        result.empty = True
        for name, label, aci in PREVIEW_LAYERS:
            result.layers.append(LayerSvg(name, label, _aci_to_hex(aci), counts[name], ""))
        return result

    # パス2: 共通の render_box で SVG 化（座標変換が全レイヤーで揃う）
    page = layout.Page(0, 0, layout.Units.mm)
    view_box: Tuple[int, int] = (0, 0)
    for index, (name, label, aci) in enumerate(PREVIEW_LAYERS):
        backend = backends.get(name)
        if backend is None:
            result.layers.append(LayerSvg(name, label, _aci_to_hex(aci), 0, ""))
            continue
        root = backend.get_xml_root_element(page, render_box=render_box)
        vb = [int(float(v)) for v in root.get("viewBox", "0 0 0 0").split()]
        view_box = (vb[2], vb[3])
        styles, groups = _prefix_classes(root, f"L{index}")
        parts = [ET.tostring(s, encoding="unicode") for s in styles]
        parts.append(f'<g class="layer" data-layer="{name}">')
        parts.extend(ET.tostring(g, encoding="unicode") for g in groups)
        parts.append("</g>")
        result.layers.append(LayerSvg(name, label, _aci_to_hex(aci), counts[name], "".join(parts)))

    result.view_box = view_box
    return result


def render_drawing_svg(dxf_bytes: bytes) -> PreviewResult:
    """任意の DXF を、元の色のまま1枚の SVG にする（「他図面の表示」用）。

    差分DXFと違いレイヤーの上書きはしないので、DXF 上で非表示（off/frozen）の画層は描かれない。
    結果は `layers` に1件（name="DRAWING"）だけ持つ。元の dxf_bytes は変更しない。
    """
    doc = _read_doc(dxf_bytes)
    cjk_font = find_cjk_font()
    if cjk_font:
        _apply_cjk_font(doc, cjk_font)

    msp = doc.modelspace()
    count = len(msp)
    result = PreviewResult(cjk_font=cjk_font)
    layer = LayerSvg("DRAWING", "図面", "#ffffff", count, "")
    result.layers.append(layer)

    backend = svg.SVGBackend()
    cfg = drawing_config.Configuration(background_policy=drawing_config.BackgroundPolicy.BLACK)
    Frontend(RenderContext(doc), backend, config=cfg).draw_layout(msp)
    bbox = backend.player().bbox()
    if not count or not bbox.has_data:
        result.empty = True
        return result

    root = backend.get_xml_root_element(layout.Page(0, 0, layout.Units.mm), render_box=bbox)
    vb = [int(float(v)) for v in root.get("viewBox", "0 0 0 0").split()]
    result.view_box = (vb[2], vb[3])
    styles, groups = _prefix_classes(root, "D0")
    parts = [ET.tostring(st, encoding="unicode") for st in styles]
    parts.append('<g class="layer" data-layer="DRAWING">')
    parts.extend(ET.tostring(g, encoding="unicode") for g in groups)
    parts.append("</g>")
    layer.inner_svg = "".join(parts)
    return result


_VIEWER_CSS = """
html,body{margin:0;height:100%;background:#000;color:#ddd;
  font-family:-apple-system,"Hiragino Sans","Yu Gothic UI","Meiryo",sans-serif;font-size:13px}
#wrap{display:flex;height:100%}
#side{width:230px;flex:none;padding:8px;box-sizing:border-box;overflow:auto;
  background:#1c1c1c;border-right:1px solid #333}
#side h4{margin:6px 0 4px;font-size:12px;color:#aaa;font-weight:600}
#side label{display:flex;align-items:center;gap:6px;margin:3px 0;cursor:pointer}
#side label.off{opacity:.4;cursor:default}
.sw{display:inline-block;width:12px;height:12px;border:1px solid #666;flex:none}
.cnt{margin-left:auto;color:#888}
button{background:#2d2d2d;color:#ddd;border:1px solid #555;border-radius:4px;
  padding:3px 8px;margin:2px 2px 2px 0;cursor:pointer;font-size:12px}
button:hover{background:#3a3a3a}
#stage{flex:1;min-width:0;position:relative}
#cv{width:100%;height:100%;display:block;cursor:grab;touch-action:none}
#cv.drag{cursor:grabbing}
#cv .layer path{vector-effect:non-scaling-stroke;stroke-width:1px}
#note{position:absolute;right:8px;bottom:6px;color:#777;font-size:11px;pointer-events:none}
#wrap:fullscreen{height:100vh !important}
"""

_VIEWER_JS = """
(function(){
  var svg=document.getElementById('cv');
  var vb0=svg.getAttribute('viewBox').split(' ').map(Number);
  var vb=vb0.slice();
  function apply(){svg.setAttribute('viewBox',vb.join(' '));}
  function reset(){vb=vb0.slice();apply();}
  function pt(e){var r=svg.getBoundingClientRect();
    var s=Math.max(vb[2]/r.width,vb[3]/r.height);
    var offx=(r.width-vb[2]/s)/2, offy=(r.height-vb[3]/s)/2;
    return {x:vb[0]+(e.clientX-r.left-offx)*s,y:vb[1]+(e.clientY-r.top-offy)*s,s:s};}
  svg.addEventListener('wheel',function(e){
    e.preventDefault();
    var p=pt(e),k=e.deltaY<0?0.8:1.25;
    var nw=vb[2]*k,nh=vb[3]*k;
    if(nw>vb0[2]*20||nw<vb0[2]/2000)return;
    vb=[p.x-(p.x-vb[0])*k,p.y-(p.y-vb[1])*k,nw,nh];apply();
  },{passive:false});
  var drag=null;
  svg.addEventListener('pointerdown',function(e){
    drag={x:e.clientX,y:e.clientY,vb:vb.slice(),s:pt(e).s};
    svg.setPointerCapture(e.pointerId);svg.classList.add('drag');});
  svg.addEventListener('pointermove',function(e){
    if(!drag)return;
    vb=[drag.vb[0]-(e.clientX-drag.x)*drag.s,drag.vb[1]-(e.clientY-drag.y)*drag.s,drag.vb[2],drag.vb[3]];apply();});
  function end(){drag=null;svg.classList.remove('drag');}
  svg.addEventListener('pointerup',end);svg.addEventListener('pointercancel',end);
  svg.addEventListener('dblclick',reset);
  document.getElementById('fit').addEventListener('click',reset);

  var wrap=document.getElementById('wrap'),fsBtn=document.getElementById('fs'),fsNote=document.getElementById('fsnote');
  function fsLabel(){fsBtn.textContent=document.fullscreenElement?'全画面を終了':'全画面';}
  fsBtn.addEventListener('click',function(){
    fsNote.textContent='';
    if(document.fullscreenElement){document.exitFullscreen();return;}
    var p=wrap.requestFullscreen?wrap.requestFullscreen():null;
    if(!p){fsNote.textContent='この環境では全画面にできません';return;}
    p.catch(function(){fsNote.textContent='この環境では全画面にできません';});});
  document.addEventListener('fullscreenchange',fsLabel);

  var boxes=Array.prototype.slice.call(document.querySelectorAll('input[data-layer]'));
  function show(name,on){
    var g=svg.querySelector('g.layer[data-layer="'+name+'"]');
    if(g)g.style.display=on?'':'none';}
  boxes.forEach(function(b){
    b.addEventListener('change',function(){show(b.getAttribute('data-layer'),b.checked);});});
  document.querySelectorAll('button[data-preset]').forEach(function(btn){
    btn.addEventListener('click',function(){
      var want=btn.getAttribute('data-preset');
      var set=want==='*'?null:want.split(',');
      boxes.forEach(function(b){
        if(b.disabled)return;
        var on=set===null||set.indexOf(b.getAttribute('data-layer'))>=0;
        b.checked=on;show(b.getAttribute('data-layer'),on);});});});
})();
"""


def build_viewer_html(result: PreviewResult, height_px: int = 720, layer_panel: bool = True) -> str:
    """自己完結した HTML（inline SVG + CSS + 素の JS）を生成する。外部参照は含まない。

    layer_panel=False のとき、左パネルは操作（全体表示・全画面）と操作方法だけにする（他図面用）。
    """
    if result.empty:
        return (
            f'<div style="height:{height_px}px;display:flex;align-items:center;'
            'justify-content:center;background:#000;color:#aaa;font-family:sans-serif">'
            "描画対象のエンティティがありません</div>"
        )

    w, h = result.view_box
    layer_rows = []
    for layer in result.layers:
        disabled = layer.count == 0
        layer_rows.append(
            f'<label class="{"off" if disabled else ""}">'
            f'<input type="checkbox" data-layer="{html.escape(layer.name)}" '
            f'{"disabled" if disabled else "checked"}>'
            f'<span class="sw" style="background:{layer.color_hex}"></span>'
            f"<span>{html.escape(layer.label)}</span>"
            f'<span class="cnt">{layer.count}</span></label>'
        )
    preset_buttons = []
    for label, names in PRESETS:
        value = "*" if names is None else ",".join(names)
        preset_buttons.append(f'<button data-preset="{html.escape(value)}">{html.escape(label)}</button>')

    if layer_panel:
        panel_top = (f'<h4>表示プリセット</h4>{"".join(preset_buttons)}'
                     f'<h4>レイヤー</h4>{"".join(layer_rows)}')
        side_style = ""
    else:
        panel_top = ""
        side_style = ' style="width:160px"'

    body = "".join(layer.inner_svg for layer in result.layers)
    font_note = "" if result.cjk_font else "日本語フォントが見つからないため、日本語の文字は表示されません。"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{_VIEWER_CSS}</style></head>
<body><div id="wrap" style="height:{height_px}px">
<div id="side"{side_style}>
{panel_top}
<h4>操作</h4><button id="fit">全体表示</button><button id="fs">全画面</button><div id="fsnote" style="color:#e8a"></div>
<div style="color:#888;margin-top:6px;line-height:1.5">ホイール: 拡大縮小<br>ドラッグ: 移動<br>ダブルクリック: 全体表示</div>
<div style="color:#e8a;margin-top:8px">{html.escape(font_note)}</div>
</div>
<div id="stage"><svg id="cv" xmlns="{SVG_NS}" viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet">
<rect x="0" y="0" width="{w}" height="{h}" fill="#000"/>{body}</svg>
<div id="note">線幅は拡大しても一定です</div></div>
</div><script>{_VIEWER_JS}</script></body></html>"""
