"""矩形領域（直交ポリゴン）検出モジュール

電気回路 DXF 内の閉領域（直交ポリゴン。四角形に限らない）を検出し、領域内
ラベルに領域名を付与する。

識別キー:
  - 図面枠      : lineweight=100 かつ color=7(ACI白) の線分
  - 領域境界線  : lineweight=25 かつ color=2(ACI黄) かつ線種が実質的に Continuous

モジュール内の構成（処理パイプラインの順）:
  1.  設定（DEFAULT_REGION_CONFIG）
  2.  DXFジオメトリ収集（_collect_region_geometry 系）
  3.  ポリゴン・点の幾何ユーティリティ（汎用、複数セクションから使われる）
  4.  線分処理の共通ユーティリティ（分類・クラスタリング・結合）
  5.  図面枠検出（detect_drawing_frames）
  6.  閉領域検出（半面探索・行き止まり枝、_detect_regions まで）
  7.  領域名称候補（Tier付き優先順位、region_name_candidates）
  8.  図面回転判定（90°回転対応）
  9.  タイトルブロック除外
  10. 領域検出実行（_run_region_detection）
  10b. 補完面解消（_resolve_complement_faces）
  11. トップレベル解析（公開API: analyze_dxf_regions, assign_region_labels）
"""
import math
import gc
import os
from collections import Counter, defaultdict

import ezdxf

from .extract_labels import extract_text_from_entity, extract_drawing_numbers
from .common_utils import filter_non_circuit_symbols, normalize_width


# ============================================================
# 1. 設定
# ============================================================

DEFAULT_REGION_CONFIG = {
    'frame_lineweight': 100,    # 図面枠の線の太さ
    'frame_color': 7,           # 図面枠の色(ACI)。lineweight=100だけでは図面枠以外の
                                # 短い無関係な線分（実例: 色5の小さな線分群）も拾ってしまうため、
                                # 色も合わせて判定する（2026-06-24、サンプル137件で検証）。
    'region_lineweight': 25,    # 領域境界線の太さ
    'region_color': 2,          # 領域境界線の色(ACI)
    'snap': 2.0,                # 軸平行判定・レベルクラスタの許容誤差
    'face_snap': 0.1,           # 矩形を構成する線分同士の接続点(交点)の座標マージン
                                # ※小さく（違う矩形を取り込むリスクを抑える）
    'merge_level_tol': 0.5,     # 共線セグメント結合時のレベル座標(縦=x/横=y)一致許容
                                # ※小さくする（別レベルの線=別矩形を結合しない）
    # ギャップ（隙間）の橋渡し方針：部品ラベルは縦線分だけを途切れさせるため、
    # 縦線分のギャップのみ橋渡しし、横線分のギャップは橋渡ししない（別矩形の取り込み防止）。
    'bridge_vertical_gaps': True,    # 縦線分(同一X)のギャップを橋渡しする
    'bridge_horizontal_gaps': False, # 横線分(同一Y)のギャップは橋渡ししない
    'corner_tol': 0.5,               # 縦線端点と横線端点が一致（コーナー）とみなす許容。
                                     # ギャップ両端にコーナー相手がいれば橋渡ししない。
    'span_level_merge': False,  # 共線結合のレベルを「スパンを構成した線分だけ」の平均で
                                # 算出する（既定はレベルクラスタ全体の平均）。レベル汚染
                                # フォールバック（analyze_dxf_regions 4パス目）が True で使う。
    'area_ratio': 0.15,         # 単独の領域の最小面積（枠面積比）
    'group_area_ratio': 0.10,   # 同名複数ピースを合算した場合の最小合計面積（枠面積比）
    'min_face_ratio': 0.005,    # 個々の閉領域として残す最小面積（枠面積比、ノイズ除去）
    'name_max_dist': 10.0,      # 名称ラベルの境界からの最大距離
    'name_min_dist': 1.0,       # 名称ラベルの境界からの最小距離（線分上=0 を除外）
    'name_min_letters': 3,      # 名称候補に必要な英字数
    'name_exclude_terms': ('NOTE', '☆'),  # 候補から除外する語（含む場合）
    'name_exclude_lowercase': True,  # 英小文字を含むラベルを名称候補から除外
    'exclude_titleblock': True, # 図番枠（タイトルブロック）を領域から除外
    'exclude_circuit_symbols': True,   # 機器符号(候補)を名称候補から除外
    'circuit_symbol_keep_terms': ('RACK',),  # この語を含むラベルは機器符号扱いしない（例 RACK1）
    'exclude_connection_point_regions': True,  # 境界に接続点(円)を持つ領域(配線ループ)を除外
    'connection_point_threshold': 1,    # 境界上の接続点がこの数(個数)以上なら除外
    'connection_point_margin': 0.05,   # 接続点が境界線上とみなす座標距離マージン
}

# 内部定数（マジックナンバーの明示）
_FRAME_MARGIN = 5       # 図面枠フィルタリング時の座標マージン（枠境界に対し ±5 単位で線分を収集）
_MAX_FACE_NODES = 200_000  # 半面探索の暴走ループ防止: 頂点数がこれを超えたら無効とみなす


# ============================================================
# 2. DXFジオメトリ収集
# ============================================================

def _is_continuous_linetype(e, doc):
    """エンティティの線種が実質的に Continuous（実線）かどうかを判定する。

    PHANTOM（二点鎖線）等の装飾的な線種は、lineweight/color が境界線条件
    （region_lineweight/region_color）に一致していても、実際の閉領域の壁を
    表すものではない（手描き図面で「別位置案」やセンターライン的な意味で使われる）。
    `EE6313-546-01E.dxf` で、本来は単一の小さな実体（handle 21AB/21AC/219A/219E、
    Continuous）である "MX CHAMBER" の周囲に、別の handle（21AE/21A1/21A9/2198等、
    PHANTOM）で描かれた二点鎖線の矩形が重なっており、これも境界線として誤認識し、
    本来存在しない「くり抜き」形状の領域が検出される不具合が報告された
    （ユーザー確認: linetype=PHANTOM の矩形は実体ではない）。`linetype='ByLayer'`
    の場合はレイヤーの既定線種まで解決する。
    """
    lt = (getattr(e.dxf, 'linetype', None) or 'BYLAYER').upper()
    if lt == 'BYLAYER':
        layer = doc.layers.get(e.dxf.layer) if doc else None
        lt = (layer.dxf.linetype if layer else 'CONTINUOUS').upper()
    return lt == 'CONTINUOUS'


def _collect_region_geometry(msp, cfg):
    """msp を1回走査し、INSERT も展開して、図面枠線・領域境界線・テキスト・
    接続点（CIRCLE を含むブロックの INSERT 位置）を収集する。"""
    frame_lines = []
    region_lines = []
    label_entities = []
    connection_points = []
    flw = cfg['frame_lineweight']
    fcol = cfg['frame_color']
    rlw = cfg['region_lineweight']
    rcol = cfg['region_color']

    doc = getattr(msp, 'doc', None)
    _circle_block = {}

    def block_has_circle(name):
        if name not in _circle_block:
            has = False
            try:
                blk = doc.blocks.get(name) if doc else None
                if blk is not None:
                    has = any(x.dxftype() == 'CIRCLE' for x in blk)
            except Exception:
                has = False
            _circle_block[name] = has
        return _circle_block[name]

    def handle_line(e, owner_handle=None):
        lw = getattr(e.dxf, 'lineweight', None)
        col = getattr(e.dxf, 'color', None)
        if lw == flw and col == fcol:
            frame_lines.append((e.dxf.start, e.dxf.end))
        elif (lw == rlw and col == rcol
              and _is_continuous_linetype(e, doc)):
            # 領域境界線のみ handle を保持する（行き止まり枝の報告用。virtual_entities()
            # 由来（INSERT 展開）は handle が None になるため、所属 INSERT の handle で代替）。
            region_lines.append((e.dxf.start, e.dxf.end, e.dxf.handle or owner_handle))

    region_lines_lp = []  # LWPOLYLINE 由来の境界線（LINE と分離して収集）

    def handle_lwpolyline_lp(e, owner_handle=None):
        """LWPOLYLINE の辺を LINE 相当として収集する（別リストへ）。"""
        lw = getattr(e.dxf, 'lineweight', None)
        if (lw != rlw or getattr(e.dxf, 'color', None) != rcol
                or not _is_continuous_linetype(e, doc)):
            return
        try:
            pts = list(e.get_points())  # (x, y, bulge, start_width, end_width)
        except Exception:
            return
        n = len(pts)
        if n < 2:
            return
        handle = e.dxf.handle or owner_handle
        close_range = n if e.closed else n - 1
        for i in range(close_range):
            p0 = pts[i]
            p1 = pts[(i + 1) % n]
            if abs(p0[2]) > 1e-6:
                continue
            region_lines_lp.append(((p0[0], p0[1]), (p1[0], p1[1]), handle))

    for e in msp:
        t = e.dxftype()
        if t == 'LINE':
            handle_line(e)
        elif t == 'LWPOLYLINE':
            handle_lwpolyline_lp(e)
        elif t in ('TEXT', 'MTEXT'):
            label_entities.append(e)
        elif t == 'INSERT':
            if block_has_circle(e.dxf.name):
                ins = e.dxf.insert
                connection_points.append((ins[0], ins[1]))
            try:
                for v in e.virtual_entities():
                    vt = v.dxftype()
                    if vt == 'LINE':
                        handle_line(v, owner_handle=e.dxf.handle)
                    elif vt == 'LWPOLYLINE':
                        handle_lwpolyline_lp(v, owner_handle=e.dxf.handle)
                    elif vt in ('TEXT', 'MTEXT'):
                        label_entities.append(v)
            except Exception:
                pass
    return frame_lines, region_lines, region_lines_lp, label_entities, connection_points


# ============================================================
# 3. ポリゴン・点の幾何ユーティリティ（汎用）
# ============================================================

def _polygon_area(poly):
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _polygon_corners(poly, tol=0.5):
    """ポリゴンの角（直角に折れる頂点）だけを抽出し、左下から順に並べて返す。

    面探索由来の共線中間点を除去し、最も左下（最小y→最小x）の角を先頭にする。
    """
    n = len(poly)
    out = []
    for i in range(n):
        p0 = poly[(i - 1) % n]
        p1 = poly[i]
        p2 = poly[(i + 1) % n]
        cross = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0])
        if abs(cross) > tol:   # 折れ点（共線でない）→ 角
            out.append((round(p1[0], 2), round(p1[1], 2)))
    if not out:
        out = [(round(x, 2), round(y, 2)) for (x, y) in poly]
    start = min(range(len(out)), key=lambda i: (out[i][1], out[i][0]))
    return out[start:] + out[:start]


def _point_in_polygon(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _dist_point_to_polygon(pt, poly):
    x, y = pt
    best = float('inf')
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        denom = dx * dx + dy * dy
        t = 0.0 if denom == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / denom))
        px, py = x1 + t * dx, y1 + t * dy
        best = min(best, math.hypot(x - px, y - py))
    return best


def _count_connection_points_on_boundary(polygon, points, margin):
    """ポリゴン境界から margin 以内にある接続点の数を返す（bbox で事前絞り込み）。"""
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    x0, x1 = min(xs) - margin - 1, max(xs) + margin + 1
    y0, y1 = min(ys) - margin - 1, max(ys) + margin + 1
    n = 0
    for (px, py) in points:
        if x0 <= px <= x1 and y0 <= py <= y1:
            if _dist_point_to_polygon((px, py), polygon) <= margin:
                n += 1
    return n


def _polygon_sample_points(poly):
    """ポリゴンの頂点＋各辺の中点を返す（重なり判定のサンプル点）。

    辺の中点を含めるのは、両ポリゴンの頂点同士は互いの外側にあるが辺が交差して
    重なっているケース（斜めにずれて一部だけ重複する等）を、頂点だけのサンプルでは
    取り落とすため。"""
    pts = list(poly)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        pts.append(((x1 + x2) / 2.0, (y1 + y2) / 2.0))
    return pts


def _polygon_has_point_strictly_inside(pts, poly, tol):
    """pts のいずれかが poly の内部に（境界から `tol` より離れて）あるか。

    単に境界に接しているだけ（隣接する領域が壁を共有する等）は重なりとみなさない。
    """
    for pt in pts:
        if _point_in_polygon(pt, poly) and _dist_point_to_polygon(pt, poly) > tol:
            return True
    return False


def regions_overlap(poly_a, poly_b, tol=1.0):
    """2つの領域ポリゴンが重なっている（一方が他方に完全に内包される場合を含む、
    部分的な重複も含む）かを判定する。

    `EE6313-546-01E.dxf` の `B CHAMBER`（外側）と `BAKE HEATER UNIT RX`（内側、
    完全内包）はもちろん検出されるが、完全な内包に限らず、面積の一部だけが
    重なっているケースも対象に含める（ユーザー要望: 内包だけでなく重なって
    いる部分があれば同期しないとすべき）。単に境界が接している（隣接して壁を
    共有するだけ）場合は重なりとはみなさない。`app.py` の他領域への名称選択
    同期（MPD RACK2 のような空間的に分離した複数ピース合算を想定）が、こうした
    重なる領域同士を誤って同期してしまう不具合の対策として使う（v1.5.11、
    ユーザー報告: 領域1/2 のどちらかでデフォルトでない候補を手動選択すると、
    もう片方も同じ名称に同期されてしまう）。"""
    pts_a = _polygon_sample_points(poly_a)
    pts_b = _polygon_sample_points(poly_b)
    return (_polygon_has_point_strictly_inside(pts_a, poly_b, tol)
            or _polygon_has_point_strictly_inside(pts_b, poly_a, tol))


# ============================================================
# 4. 線分処理の共通ユーティリティ（分類・クラスタリング・結合）
# ============================================================

def _split_axis_aligned(pairs, eps):
    """線分(start,end[,handle])を水平 H[(y,x0,x1)] と垂直 V[(x,y0,y1)] に分類する。

    末尾の handle 要素（行き止まり枝の報告用に region_lines/region_lines_lp が
    持つ）は無視する。2要素(frame_lines)・3要素(region_lines系)のいずれも扱える。
    """
    H = []
    V = []
    for item in pairs:
        s, en = item[0], item[1]
        x1, y1, x2, y2 = s[0], s[1], en[0], en[1]
        if abs(y1 - y2) <= eps and abs(x1 - x2) >= eps:
            H.append(((y1 + y2) / 2.0, min(x1, x2), max(x1, x2)))
        elif abs(x1 - x2) <= eps and abs(y1 - y2) >= eps:
            V.append(((x1 + x2) / 2.0, min(y1, y2), max(y1, y2)))
    return H, V


def _cluster_1d(vals, tol):
    vals = sorted(vals)
    out = []
    cur = [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] <= tol:
            cur.append(v)
        else:
            out.append(sum(cur) / len(cur))
            cur = [v]
    out.append(sum(cur) / len(cur))
    return out


def _gap_has_circle(level, a, b, circles, band):
    """縦線分(level=x)のギャップ [a,b]（y方向）に接続点(円)が乗っているか判定する。"""
    if not circles:
        return False
    for (cx, cy) in circles:
        if abs(cx - level) <= band and a - band <= cy <= b + band:
            return True
    return False


def _has_corner_partner(level, y, h_endpoints, tol):
    """縦線端点 (level, y) に、横線分の端点が一致しているか（＝コーナー相手がいるか）。
    コーナー相手がいる端点は境界がそこで折れるので、ギャップ橋渡ししない。"""
    for (hx, hy) in (h_endpoints or ()):
        if abs(hx - level) <= tol and abs(hy - y) <= tol:
            return True
    return False


def _merge_collinear(items, level_tol, bridge=True, circles=None, circle_band=2.0,
                     h_endpoints=None, corner_tol=0.5, span_levels=False):
    """同一レベル(±level_tol)の共線セグメントを結合する。

    bridge=True のとき隙間（ギャップ）も橋渡しして1本にする（部品で途切れた縦線分の
    復元用）。bridge=False のときは重なり/接触するセグメントのみ結合し、隙間は別スパン
    として残す（横線分。別矩形の取り込みを防ぐ）。

    縦線のギャップ橋渡しは、**ギャップ両端のどちらにも横線分の端点が一致しない**場合
    のみ行う（端点が一致する＝コーナーで境界が折れるステップなので橋渡ししない。これに
    より、別境界片や段差を誤って繋がない）。circles がギャップ上にある場合も橋渡ししない。

    span_levels=True のとき、出力スパンのレベルを「そのスパンを構成した線分だけ」の
    平均で算出する（既定 False はレベルクラスタ全体の平均）。既定の全体平均は、スパンが
    重ならない無関係な近接線分（例: 境界線 y=122.00 の 0.37 上に乗ったコネクタ箱の底辺
    y=122.37）にレベルを汚染され、境界線がシフト → 縦線端点とのノード接続（face_snap）
    が切れて閉領域が不成立になることがある（EE6892-039-05B.dxf 2枠目で実証）。
    `analyze_dxf_regions` のレベル汚染フォールバック（4パス目）が True で再検出する。
    ギャップのコーナー相手・CIRCLE 判定は従来どおりクラスタ全体平均レベルで行う
    （判定許容誤差に対して汚染幅は level_tol 以下なので結果は変わらない）。
    """
    if not items:
        return []
    items = sorted(items, key=lambda t: t[0])
    groups = []
    cur = [items[0]]
    for it in items[1:]:
        if it[0] - cur[-1][0] <= level_tol:
            cur.append(it)
        else:
            groups.append(cur)
            cur = [it]
    groups.append(cur)

    out = []
    for g in groups:
        level = sum(t[0] for t in g) / len(g)
        # merged 要素: [lo, hi, [構成線分のレベル, ...]]
        spans = sorted((t[1], t[2], t[0]) for t in g)
        merged = [[spans[0][0], spans[0][1], [spans[0][2]]]]
        for lo, hi, lv in spans[1:]:
            phi = merged[-1][1]
            if lo <= phi + 1e-6:  # 重なり/接触 → 結合
                merged[-1][1] = max(phi, hi)
                merged[-1][2].append(lv)
            elif (bridge
                  and not _has_corner_partner(level, phi, h_endpoints, corner_tol)
                  and not _has_corner_partner(level, lo, h_endpoints, corner_tol)
                  and not _gap_has_circle(level, phi, lo, circles, circle_band)):
                merged[-1][1] = max(phi, hi)  # 橋渡し（両端コーナー無し・円無し）
                merged[-1][2].append(lv)
            else:
                merged.append([lo, hi, [lv]])  # 隙間 → 別スパンとして分離
        for lo, hi, lvs in merged:
            out.append((sum(lvs) / len(lvs) if span_levels else level, lo, hi))
    return out


# ============================================================
# 5. 図面枠検出
# ============================================================

def detect_drawing_frames(
    frame_lines: list,
    eps: float = 2.0,
    min_side: float = 0.0,
) -> list[tuple[float, float, float, float]]:
    """lineweight=100・color=7 の線分（呼び出し元の `_collect_region_geometry` で
    既にこの2条件で絞り込まれている）から図面枠（複数可）を検出する。
    枠の縦長辺が左右ペアで横並びになる前提。戻り値: [(xl,xr,y0,y1), ...]

    注: 枠の縦辺が複数線分に分断されている場合（例: ブロック内で line が分割されて
    いるケース）でも正しく検出できるよう、分類後に共線セグメントを結合してから
    高さ判定を行う。

    `min_side`（既定0=フィルタなし）: 2026-06-24以前は400.0固定で、縦辺の高さが
    これ未満の枠（実例: EE6097-039-06C.dxf、高さ277）を取り落としていた。
    color=7 条件を追加導入したことで、無関係な短い lineweight=100 線分（実例:
    色5の小さな線分群、サンプル137件で確認）が混入しなくなったため、高さに
    よる足切りは不要になった。
    """
    _, V = _split_axis_aligned(frame_lines, eps)
    # 接触/重複する同一 x 上の線分を 1 本に統合してから高さ判定
    # （例: EE6888-631-01A.dxf の右辺が y=367.5 で 2 分割されているケース）
    Vm = _merge_collinear(V, eps, bridge=False)
    tall = [v for v in Vm if (v[2] - v[1]) >= min_side]
    if len(tall) < 2:
        return []
    xedges = _cluster_1d([v[0] for v in tall], eps)
    ys = [v[1] for v in tall] + [v[2] for v in tall]
    y0, y1 = min(ys), max(ys)
    frames = []
    for i in range(0, len(xedges) - 1, 2):
        frames.append((xedges[i], xedges[i + 1], y0, y1))
    return frames


# ============================================================
# 6. 閉領域検出（半面探索・行き止まり枝）
# ============================================================

def _build_planar_graph(Hm, Vm, eps):
    """結合済み水平線 Hm[(y,x0,x1)]・垂直線 Vm[(x,y0,y1)] から、端点接続ベースの
    平面グラフ（隣接リスト adj とノード座標 node_xy）を構築する。

    接続は **線分の端点が相手の線分に乗っている箇所（角・T字）のみ** で作る。
    中ほど同士の交差（どちらの端点でもない交差）では接続しない。これにより、
    コネクタ横線が矩形右辺の途中を横切るだけの箇所で誤って繋がるのを防ぐ。
    座標は許容誤差クラスタリングで正規化する（round の境界で一致点が分裂するのを
    防ぐ。手描きの微小ズレ、例 y=231.91 と 231.96 を同一ノードに寄せる）。

    戻り値: (adj, node_xy)。adj は {node_key: {隣接node_key, ...}} の隣接リスト
    （無向グラフ、双方向に登録）。node_xy は {node_key: (x, y)} の実座標。
    """
    ctol = max(eps, 0.2)

    def _canon_map(values):
        sv = sorted(set(values))
        m = {}
        if not sv:
            return m
        cluster = [sv[0]]
        for v in sv[1:]:
            if v - cluster[-1] <= ctol:
                cluster.append(v)
            else:
                c = sum(cluster) / len(cluster)
                for u in cluster:
                    m[u] = c
                cluster = [v]
        c = sum(cluster) / len(cluster)
        for u in cluster:
            m[u] = c
        return m

    all_x = set()
    all_y = set()
    for (y, x0, x1) in Hm:
        all_y.add(y); all_x.add(x0); all_x.add(x1)
    for (x, y0, y1) in Vm:
        all_x.add(x); all_y.add(y0); all_y.add(y1)
    cx = _canon_map(all_x)
    cy = _canon_map(all_y)

    def cluster_key(x, y):
        return (round(cx[x], 3), round(cy[y], 3))

    v_endpoints = []
    for (x, y0, y1) in Vm:
        v_endpoints.append((x, y0))
        v_endpoints.append((x, y1))
    h_endpoints = []
    for (y, x0, x1) in Hm:
        h_endpoints.append((x0, y))
        h_endpoints.append((x1, y))

    node_xy = {}
    line_pts = {}
    # 横線上のノード = 自身の端点 ＋ そこに端点で接する縦線の位置
    for hi, (y, x0, x1) in enumerate(Hm):
        xs = [x0, x1]
        for (vx, vy) in v_endpoints:
            if x0 - eps <= vx <= x1 + eps and abs(vy - y) <= eps:
                xs.append(vx)
        for x in xs:
            k = cluster_key(x, y)
            node_xy[k] = (x, y)
            line_pts.setdefault(('H', hi), []).append((x, k))
    # 縦線上のノード = 自身の端点 ＋ そこに端点で接する横線の位置
    for vi, (x, y0, y1) in enumerate(Vm):
        ys = [y0, y1]
        for (hx, hy) in h_endpoints:
            if y0 - eps <= hy <= y1 + eps and abs(hx - x) <= eps:
                ys.append(hy)
        for yy in ys:
            k = cluster_key(x, yy)
            node_xy[k] = (x, yy)
            line_pts.setdefault(('V', vi), []).append((yy, k))

    adj = {}
    for pts in line_pts.values():
        pts = sorted(set(pts))
        for a in range(len(pts) - 1):
            ka, kb = pts[a][1], pts[a + 1][1]
            if ka != kb:
                adj.setdefault(ka, set()).add(kb)
                adj.setdefault(kb, set()).add(ka)
    return adj, node_xy


def _peel_dangling_branches(adj, node_xy):
    """次数1のノード（行き止まり）とその辺を再帰的に除去する（2-core抽出）。

    半面探索は次数1のノードに到達すると、戻る辺が1本しかないため必ず同じ辺を
    折り返す。この往復が生のポリゴンに「同じ座標が2回連続する」アーティファクトを
    生む（面積には寄与しないが、頂点座標の表示を汚す）。真の境界閉路は必ず次数2
    以上のノードのみで構成されるため、面探索前にここで除去する。

    `adj` は呼び出し側の辞書を**直接変更**する（除去後のグラフを面探索に渡すため）。

    除去した辺は「枝（連結成分）」単位にまとめて返す。1本の枝が複数の短い線分の
    連なりで構成される場合（部品が複数回切れ目を入れている、あるいは1本の長い
    線が途中まで領域境界として使われ残りが余剰になっている等）も、先端から
    現存グラフへの取り付け点までを1つの枝として扱う（Union-Find で連結成分化）。

    戻り値: [{'edges': [(座標, 座標), ...], 'attachment': 座標 | None}, ...]
    """
    peeled_pairs = []  # (leaf_key, other_key)、除去順
    changed = True
    while changed:
        changed = False
        leaves = [n for n, nbrs in adj.items() if len(nbrs) == 1]
        for leaf in leaves:
            nbrs = adj.get(leaf)
            if not nbrs:
                continue
            other = next(iter(nbrs))
            peeled_pairs.append((leaf, other))
            adj[other].discard(leaf)
            if not adj[other]:
                del adj[other]
            del adj[leaf]
            changed = True

    if not peeled_pairs:
        return []

    parent = {}

    def _uf_find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    def _uf_union(a, b):
        ra, rb = _uf_find(a), _uf_find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in peeled_pairs:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        _uf_union(a, b)

    groups = {}
    for a, b in peeled_pairs:
        groups.setdefault(_uf_find(a), []).append((a, b))

    dangling_branches = []
    for edges in groups.values():
        keys = {k for ab in edges for k in ab}
        attach_keys = [k for k in keys if k in adj]
        dangling_branches.append({
            'edges': [(node_xy[a], node_xy[b]) for a, b in edges],
            'attachment': node_xy[attach_keys[0]] if attach_keys else None,
        })
    return dangling_branches


def _trace_faces(adj, node_xy):
    """2-core抽出済みの平面グラフ（adj, node_xy）から、半面探索で閉領域(面)を
    列挙する。各面は次数2以上のノードのみで構成される閉路（半面探索＝各有向辺を
    1回ずつ辿り、各ノードで「来た方向の直前（角度順で1つ前）」の隣接辺へ進む）。"""
    def ang(a, b):
        ax, ay = node_xy[a]
        bx, by = node_xy[b]
        return math.atan2(by - ay, bx - ax)

    order = {n: sorted(nb, key=lambda mm: ang(n, mm)) for n, nb in adj.items()}
    visited = set()
    faces = []
    for u in list(adj.keys()):
        for v in adj[u]:
            if (u, v) in visited:
                continue
            face = []
            cu, cv = u, v
            ok = True
            while True:
                visited.add((cu, cv))
                face.append(node_xy[cu])
                nb = order[cv]
                w = nb[(nb.index(cu) - 1) % len(nb)]
                cu, cv = cv, w
                if (cu, cv) == (u, v):
                    break
                if len(face) > _MAX_FACE_NODES:
                    ok = False
                    break
            if ok and len(face) >= 4:
                faces.append(face)
    return faces


def _find_rectilinear_faces(Hm, Vm, eps):
    """結合済み水平線 Hm[(y,x0,x1)]・垂直線 Vm[(x,y0,y1)] から閉領域(面)と
    行き止まり枝を求める。

    `_build_planar_graph`（平面グラフ構築）→ `_peel_dangling_branches`
    （行き止まり枝の除去・連結成分化）→ `_trace_faces`（半面探索）の3段の
    オーケストレーション。戻り値: (faces, dangling_branches)。
    """
    adj, node_xy = _build_planar_graph(Hm, Vm, eps)
    if not adj:
        return [], []
    dangling_branches = _peel_dangling_branches(adj, node_xy)
    if not adj:
        return [], dangling_branches
    faces = _trace_faces(adj, node_xy)
    return faces, dangling_branches


def _resolve_dangling_handles(dangling_branches, raw_lines, tol=0.5):
    """行き止まり枝（枝ごとの端点ペアのリスト＋取り付け点）について、その経路上に
    ある元のLINE/LWPOLYLINE辺の handle と実座標（クラスタ正規化前）を解決する。

    raw_lines は (start, end, handle) の3要素タプルのリスト
    （`_collect_region_geometry` の region_lines / region_lines_lp）。
    1本の枝が複数の生エンティティ（複数の短い線分の連なり、または閉領域の境界
    として一部だけ使われている1本の長い線の余剰部分）で構成される場合も、枝を
    構成する全セグメントの延長線上にあり区間が重なる全エンティティを対象に含める
    （セグメントをまたいで同じ handle が重複してもエンティティ単位で重複除去する）。
    """
    results = []
    for branch in dangling_branches:
        entities = []
        seen = set()
        for (p1, p2) in branch['edges']:
            x1, y1 = p1
            x2, y2 = p2
            vertical = abs(x1 - x2) <= tol
            level = (x1 + x2) / 2.0 if vertical else (y1 + y2) / 2.0
            lo, hi = (min(y1, y2), max(y1, y2)) if vertical else (min(x1, x2), max(x1, x2))
            for (s, en, handle) in raw_lines:
                sx, sy = s[0], s[1]
                ex, ey = en[0], en[1]
                if vertical:
                    if abs(sx - level) > tol or abs(ex - level) > tol:
                        continue
                    seg_lo, seg_hi = min(sy, ey), max(sy, ey)
                else:
                    if abs(sy - level) > tol or abs(ey - level) > tol:
                        continue
                    seg_lo, seg_hi = min(sx, ex), max(sx, ex)
                if seg_hi < lo - tol or seg_lo > hi + tol:
                    continue
                key = handle if handle else (round(sx, 3), round(sy, 3), round(ex, 3), round(ey, 3))
                if key in seen:
                    continue
                seen.add(key)
                entities.append({
                    'handle': handle,
                    'start': (round(sx, 2), round(sy, 2)),
                    'end': (round(ex, 2), round(ey, 2)),
                })
        attachment = branch.get('attachment')
        results.append({
            'attachment': (round(attachment[0], 2), round(attachment[1], 2)) if attachment else None,
            'entities': entities,
        })
    return results


def _detect_regions(RH, RV, frame, frame_area, cfg, circles=None, raw_lines=None):
    """1つの図面枠内で、面積>=枠面積×area_ratio の閉領域を検出する。

    戻り値: (regions, dangling) のタプル。dangling は `_resolve_dangling_handles`
    の出力形式（枝＝連結成分ごとの attachment/handle/座標）。どの領域に関係する
    枝かの絞り込みは呼び出し側（`analyze_dxf_regions`）が `attachment` 座標と
    各領域の polygon で行う。
    """
    xl, xr, y0, y1 = frame
    m = _FRAME_MARGIN
    Hf = [h for h in RH if y0 - m <= h[0] <= y1 + m and h[2] >= xl - m and h[1] <= xr + m]
    Vf = [v for v in RV if xl - m <= v[0] <= xr + m and v[2] >= y0 - m and v[1] <= y1 + m]
    if not Hf or not Vf:
        return [], []
    # 共線セグメントの結合はレベル座標を厳密一致(merge_level_tol)で行い、別レベルの
    # 線（=別矩形）を誤って繋がない。ギャップ橋渡しは既定で縦線分のみ（部品ラベルは
    # 縦線分を途切れさせる）。横線分のギャップは既定では橋渡ししない。接続点(交点)判定
    # は face_snap。ギャップが CIRCLE で繋がっている場合は橋渡ししない（配線ループ除外）。
    # 図面全体が90°回転しているファイルでは部品が横線分を途切れさせるため、
    # bridge_horizontal_gaps=True 指定時は縦線分の端点をコーナー相手として
    # （x/y を入れ替えて）同じ安全条件で橋渡しする（_detect_regions を呼ぶ側が
    # 候補ゼロ時のフォールバックとして有効化する）。
    mtol = cfg.get('merge_level_tol', 0.5)
    fsnap = cfg.get('face_snap', 0.1)
    bridge_v = cfg.get('bridge_vertical_gaps', True)
    bridge_h = cfg.get('bridge_horizontal_gaps', False)
    cband = cfg.get('connection_point_margin', 2.0)
    ctol = cfg.get('corner_tol', 0.5)
    fcircles = [c for c in (circles or []) if xl - m <= c[0] <= xr + m and y0 - m <= c[1] <= y1 + m]
    # 横線分の端点（縦ギャップのコーナー相手判定用）
    h_endpoints = []
    for (hy, hx0, hx1) in Hf:
        h_endpoints.append((hx0, hy))
        h_endpoints.append((hx1, hy))
    # 縦線分の端点（横ギャップのコーナー相手判定用。x/y を入れ替えて _has_corner_partner
    # ／_gap_has_circle の (level, 位置) 引数順に合わせる）
    v_endpoints_swapped = []
    for (vx, vy0, vy1) in Vf:
        v_endpoints_swapped.append((vy0, vx))
        v_endpoints_swapped.append((vy1, vx))
    circles_swapped = [(cy, cx) for (cx, cy) in fcircles]
    span_levels = cfg.get('span_level_merge', False)
    Hm = _merge_collinear(Hf, mtol, bridge=bridge_h, circles=circles_swapped, circle_band=cband,
                          h_endpoints=v_endpoints_swapped, corner_tol=ctol,
                          span_levels=span_levels)
    Vm = _merge_collinear(Vf, mtol, bridge=bridge_v, circles=fcircles, circle_band=cband,
                          h_endpoints=h_endpoints, corner_tol=ctol,
                          span_levels=span_levels)
    # 端点接続ベースの面探索（中ほど交差では繋がない）ため、部品矩形の縦線は領域辺の
    # 途中を横切るだけで接続せず、回り込みは発生しない。
    faces, dangling = _find_rectilinear_faces(Hm, Vm, fsnap)
    thr = frame_area * cfg.get('min_face_ratio', 0.005)
    regions = []
    seen = set()
    for f in sorted(faces, key=_polygon_area, reverse=True):
        a = _polygon_area(f)
        if a < thr:
            continue
        xs = [p[0] for p in f]
        ys = [p[1] for p in f]
        bb = (round(min(xs)), round(max(xs)), round(min(ys)), round(max(ys)))
        if bb in seen:
            continue
        seen.add(bb)
        regions.append({'polygon': f, 'area': a})
    dangling_resolved = _resolve_dangling_handles(dangling, raw_lines or [])
    return regions, dangling_resolved


# ============================================================
# 7. 領域名称候補（Tier付き優先順位）
# ============================================================

def _is_letter(ch):
    """半角・全角の英字（大小問わず）かどうかを判定する。

    手書き回路DXFには領域名がすべて全角（例: `ＳＹＳＴＥＭ　Ｉ／Ｆ　ＢＯＸ`）で
    書かれている図面があり、ASCII 限定判定では英字0字とみなされ
    `name_min_letters` 条件で常に除外されていた（`is_single_uppercase_letter()`
    の全角対応と同じ考え方）。
    """
    if ch.isascii() and ch.isalpha():
        return True
    return 'Ａ' <= ch <= 'Ｚ' or 'ａ' <= ch <= 'ｚ'


def _is_lowercase_letter(ch):
    if 'a' <= ch <= 'z':
        return True
    return 'ａ' <= ch <= 'ｚ'


def _count_letters(s):
    return sum(1 for ch in s if _is_letter(ch))


def _is_valid_name_candidate(t, min_letters, exclude_lowercase, exclude_terms,
                              exclude_circuit_symbols, circuit_keep_terms):
    """領域名候補ラベルとして有効かを返す（ポリゴン非依存フィルタ）。

    region_name_candidates() と _name_union_parent() の両方で使われる共通判定。
    """
    if _count_letters(t) < min_letters:
        return False
    if exclude_lowercase and any(_is_lowercase_letter(ch) for ch in t):
        return False
    # 除外語・keep-term の照合は半角相当で行う（機器符号フィルタ側も
    # normalize_width で判定するため、全角 ＲＡＣＫ１ 等が「機器符号として
    # 除外されるのに keep-term は素通り」という不整合を防ぐ）。
    up = normalize_width(t).upper()
    if any(term.upper() in up for term in (exclude_terms or ())):
        return False
    if exclude_circuit_symbols and not any(k.upper() in up for k in (circuit_keep_terms or ())):
        matched, _ = filter_non_circuit_symbols([t])
        if matched:
            return False
    return True


def _horizontal_edges_at_extreme(polygon, side, level_tol=2.0):
    """ポリゴンの下端(side='bottom')または上端(side='top')にある横エッジ群
    [(x0,x1,y), ...] を返す（`_vertical_edges_at_extreme` の横版）。"""
    ys = [p[1] for p in polygon]
    target_y = min(ys) if side == 'bottom' else max(ys)
    segs = []
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if abs(y1 - y2) < 0.5 and abs(y1 - target_y) <= level_tol:
            segs.append((min(x1, x2), max(x1, x2), y1))
    return segs


def _bottom_edges(polygon, level_tol=2.0):
    """ポリゴンの下端（最小y）にある横エッジ群 [(x0,x1,y), ...] を返す。"""
    return _horizontal_edges_at_extreme(polygon, 'bottom', level_tol)


def _top_edges(polygon, level_tol=2.0):
    """ポリゴンの上端（最大y）にある横エッジ群 [(x0,x1,y), ...] を返す（`_bottom_edges`の上端版）。"""
    return _horizontal_edges_at_extreme(polygon, 'top', level_tol)


def _notch_bottom_edges(polygon, level_tol=2.0, probe=0.5):
    """最下端レベル以外にある下向き横エッジ群 [(x0,x1,y), ...] を返す。

    「下向き」＝エッジ中点の probe 直上が領域内・probe 直下が領域外。
    長方形では常に空（下向きエッジは最下端のみ）で、L字型等の非矩形ポリゴンの
    切り欠き部の横エッジだけが該当する。実例: EE6491-039-04A.dxf の
    SYSTEM I/F BOX。FLAT CABLE 部と一体のL字型領域で、名称ラベルが切り欠き部の
    下向きエッジ（最下端ではない）の直上にあるため、最下端エッジ（Tier1）と
    上端エッジ（Tier2）だけを見る従来の探索では候補から漏れていた。
    `region_name_candidates` が Tier2 スキャンにこのエッジ群を加えて使う。
    """
    min_y = min(p[1] for p in polygon)
    segs = []
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if abs(y1 - y2) >= 0.5:
            continue
        my = (y1 + y2) / 2.0
        if abs(my - min_y) <= level_tol:
            continue  # 最下端レベルは Tier1（_bottom_edges）の担当
        x0, x1s = min(x1, x2), max(x1, x2)
        if x1s - x0 < probe:  # 極小エッジは内外判定が不安定なため除外
            continue
        mx = (x0 + x1s) / 2.0
        if (_point_in_polygon((mx, my + probe), polygon)
                and not _point_in_polygon((mx, my - probe), polygon)):
            segs.append((x0, x1s, my))
    return segs


def _vertical_edges_at_extreme(polygon, side, level_tol=2.0):
    """ポリゴンの左端(side='left')または右端(side='right')にある縦エッジ群
    [(y0,y1,x), ...] を返す（図面全体が90°回転している場合の下端/上端の代替）。"""
    xs = [p[0] for p in polygon]
    target_x = min(xs) if side == 'left' else max(xs)
    segs = []
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if abs(x1 - x2) < 0.5 and abs(x1 - target_x) <= level_tol:
            segs.append((min(y1, y2), max(y1, y2), x1))
    return segs


def _dist_to_edge_segments(pt, segs, axis='y'):
    """点からエッジ群までの最短距離。`axis='y'`（既定）は横エッジ群
    `[(x0,x1,y), ...]` への距離（`_dist_to_bottom_edge` 相当）、`axis='x'` は
    縦エッジ群 `[(y0,y1,x), ...]` への距離（`_dist_to_vertical_edge` 相当）。
    縦版は点・エッジとも (主軸, 固定軸) の順に座標を入れ替えて同じ計算式を流用する。
    """
    a, b = pt if axis == 'y' else pt[::-1]
    best = float('inf')
    for (a0, a1, eb) in segs:
        if a0 <= a <= a1:
            d = abs(b - eb)
        else:
            d = math.hypot(a - (a0 if a < a0 else a1), b - eb)
        best = min(best, d)
    return best


def _dist_to_bottom_edge(pt, bottom_segs):
    """点から下端横エッジ群までの最短距離。"""
    return _dist_to_edge_segments(pt, bottom_segs, axis='y')


def _dist_to_vertical_edge(pt, vertical_segs):
    """点から縦エッジ群までの最短距離（_dist_to_bottom_edge の縦版）。"""
    return _dist_to_edge_segments(pt, vertical_segs, axis='x')


def region_name_candidates(
    polygon: list[tuple[float, float]],
    labels: list[tuple[str, float, float]],
    max_dist: float = 10.0,
    min_dist: float = 1.0,
    min_letters: int = 3,
    limit: int = 8,
    exclude_circuit_symbols: bool = True,
    exclude_terms: tuple = ('NOTE', '☆'),
    exclude_lowercase: bool = True,
    circuit_keep_terms: tuple = ('RACK',),
    rotated_edge_roles: tuple | None = None,
) -> tuple[list[tuple[float, str]], dict[str, int]]:
    """領域名候補ラベルを優先順位（Tier）→距離順に返す（テキスト重複除去）。

    優先順位（ユーザー確認による仕様、2026-06-21 v1.5.9）:
      Tier 1: 矩形領域内にあり、下端横エッジの最近傍（`rotated_edge_roles` 指定時は
              その1番目の側の縦エッジ＝図面回転時の下端相当。実例で確認済み:
              `DE5434-553-10B.dxf` のような回転角+90°多数派の図面では右端）
      Tier 2: 矩形領域内にあり、上端横エッジの最近傍（`rotated_edge_roles` 指定時は
              2番目の側の縦エッジ＝上端相当。回転角+90°多数派の図面では左端）
      Tier 3: Tier 1/2 のいずれでも候補が見つからない場合のみ、ポリゴン全体の境界
              （任意の辺）への最短距離でフォールバック評価する（領域内外を問わない）。
    各 Tier 内は距離が近い順。Tier1/2 はいずれも `min_dist`未満（境界線分上＝
    部品符号等が偶然乗っただけの無関係なラベル）を除外する。同じテキストが複数
    Tier・複数距離で見つかった場合は、最も優先度の高い Tier・距離のものを残す。
    `rotated_edge_roles=None`（通常図面、または回転方向が判定できない図面）の
    場合、Tier1=下端横エッジ、Tier2=上端横エッジを使う。

    Tier1/2 を**領域内側のラベルに限定する**理由（2026-06-21 追加）: 領域名は
    通常その箱の内側に書かれるため、Tier1/2 が想定する「自分の箱の名前」は内側の
    ラベルである。領域の外側にある別の箱・別の注記等のラベルが、たまたま
    Tier1/2 のエッジ（下端/上端、回転時は右端/左端）に近いという理由だけで
    内側の正しいラベルより優先されてしまう不具合があった（`DE5434-553-10B.dxf`
    の回転領域で、領域外の `EFEM UPPER`〈距離3.9〉が領域内の正しい名称
    `CONTROL BOX CORE FX`〈距離5.2〉より優先されていた。DXF-viewer の Search
    Boundary を「最上位候補のみで照合」するよう変更した際にユーザーが発見）。
    Tier3 のフォールバックは領域内外を問わない（Tier1/2 で候補が無い場合の
    最後の手段のため、範囲を絞らない）。
    条件:
      - 英字 min_letters 字以上
      - exclude_terms のいずれかを含むラベル（例 NOTE, ☆）は除外
      - exclude_lowercase=True のとき英小文字を含むラベルは除外（領域名は大文字）
      - exclude_circuit_symbols=True のとき機器符号（候補）パターン一致は除外

    戻り値: (candidates, tier_by_text) のタプル。
      candidates: [(distance, text), ...]（従来通り。distance は採用された
                  Tier での距離）
      tier_by_text: {text: tier(1|2|3), ...}（`candidates` と同じ重複除去後の
                    エントリに対応。呼び出し側が「この候補がどれだけ確信度が
                    高いか」を判定するために使う）
    """
    def _scan(edge_segs, dist_fn, require_inside):
        cand = []
        for (t, x, y) in labels:
            if not _is_valid_name_candidate(t, min_letters, exclude_lowercase,
                                            exclude_terms, exclude_circuit_symbols,
                                            circuit_keep_terms):
                continue
            if require_inside and not _point_in_polygon((x, y), polygon):
                continue
            d = dist_fn((x, y), edge_segs)
            if min_dist <= d <= max_dist:
                cand.append((d, t))
        return cand

    if rotated_edge_roles:
        tier1_side, tier2_side = rotated_edge_roles
        tier1_edges = _vertical_edges_at_extreme(polygon, tier1_side)
        tier2_edges = _vertical_edges_at_extreme(polygon, tier2_side)
        dist_fn = _dist_to_vertical_edge
    else:
        tier1_edges = _bottom_edges(polygon)
        # Tier2 は上端エッジに加え、L字型等の切り欠き部の下向きエッジも対象にする
        # （切り欠き直上の名称ラベルを拾う。詳細は _notch_bottom_edges docstring）。
        tier2_edges = _top_edges(polygon) + _notch_bottom_edges(polygon)
        dist_fn = _dist_to_bottom_edge

    tiered = []
    for tier, edges in ((1, tier1_edges), (2, tier2_edges)):
        if not edges:
            continue
        for d, t in _scan(edges, dist_fn, True):
            tiered.append((tier, d, t))

    # Tier1/2 でも候補ゼロの場合のみ、ポリゴン全体の境界への最短距離でフォールバック
    if not tiered:
        for (t, x, y) in labels:
            if not _is_valid_name_candidate(t, min_letters, exclude_lowercase,
                                            exclude_terms, exclude_circuit_symbols,
                                            circuit_keep_terms):
                continue
            d = _dist_point_to_polygon((x, y), polygon)
            if min_dist <= d <= max_dist:
                tiered.append((3, d, t))

    tiered.sort(key=lambda c: (c[0], c[1]))
    seen = set()
    out = []
    tier_by_text = {}
    for tier, d, t in tiered:
        if t in seen:
            continue
        seen.add(t)
        out.append((round(d, 1), t))
        tier_by_text[t] = tier
        if len(out) >= limit:
            break
    return out, tier_by_text


# ============================================================
# 8. 図面回転判定（90°回転対応）
# ============================================================

def _label_rotation_angle(entity):
    """ラベルエンティティの実効回転角(度, 0-180で正規化前)を返す。
    MTEXT は rotation 属性ではなく text_direction ベクトルで回転が表現される
    ことがあるため、そちらを優先する。"""
    if entity.dxftype() == 'MTEXT':
        try:
            if entity.dxf.hasattr('text_direction'):
                td = entity.dxf.get('text_direction')
                return math.degrees(math.atan2(td[1], td[0]))
        except Exception:
            pass
    return getattr(entity.dxf, 'rotation', 0) or 0


def _is_globally_rotated(label_entities, threshold=0.5):
    """ラベル(TEXT/MTEXT)の過半数が90°(または270°)回転しているか判定する。

    図面全体が90°回転して描かれたファイルでは、部品が横線分（本来の縦線分に
    相当）を途切れさせるため、横線分ギャップ橋渡しが必要になる。しかし通常向き
    の図面で「単純に検出ゼロ件だったから」を条件に橋渡しを許可すると、無関係な
    隣接矩形を誤って結合する副作用の恐れがある。そこでラベルの回転状況から
    図面全体の回転を明示的に判定し、回転図面のときのみ橋渡しを許可する。
    通常図面ではラベル回転はほぼ0%（実データで0〜0.2%程度）、回転図面では
    大半（実データで60〜97%）が90°回転していることを確認済み。
    """
    total = 0
    rotated = 0
    for e in label_entities:
        if e.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        total += 1
        ang = _label_rotation_angle(e) % 180.0
        if 80.0 <= ang <= 100.0:
            rotated += 1
    if total == 0:
        return False
    return (rotated / total) >= threshold


def _rotated_edge_roles(label_entities, threshold=0.5):
    """図面全体が90°回転している場合、下端相当/上端相当がどちら側の縦エッジに
    対応するかを判定する。

    `_is_globally_rotated` は回転の有無（角度が90°付近かどうか、符号を区別しない
    `% 180`）しか見ないが、名称候補の優先順位（下端相当を優先1位、上端相当を
    優先2位とする）には回転方向の符号（+90° か -90° か）が必要。

    実例で確認済みの対応（`DE5434-553-10B.dxf`、回転角+90°が多数派）:
      下端相当 = 右端の縦エッジ、上端相当 = 左端の縦エッジ
    回転角-90°が多数派の場合は左右が反転する（下端相当=左端、上端相当=右端）。

    戻り値: (tier1_side, tier2_side) のタプル（'left'/'right'）。回転していない、
    または回転方向の多数派が判定できない場合は None。
    """
    total = 0
    near_plus90 = 0
    near_minus90 = 0
    for e in label_entities:
        if e.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        total += 1
        ang = _label_rotation_angle(e)
        ang = ((ang + 180.0) % 360.0) - 180.0  # (-180, 180] に正規化
        if 80.0 <= ang <= 100.0:
            near_plus90 += 1
        elif -100.0 <= ang <= -80.0:
            near_minus90 += 1
    if total == 0:
        return None
    if (near_plus90 / total) >= threshold:
        return ('right', 'left')
    if (near_minus90 / total) >= threshold:
        return ('left', 'right')
    return None


# ============================================================
# 9. タイトルブロック除外
# ============================================================

def _is_titleblock_region(polygon, labels):
    """領域内に図番パターンとタイトル系語が同居していれば図番枠とみなす。"""
    has_dn = False
    has_term = False
    terms = ('TITLE', 'REVISION', 'DWG', '流用元', '図番')
    for (t, x, y) in labels:
        if not _point_in_polygon((x, y), polygon):
            continue
        if not has_dn and extract_drawing_numbers(t):
            has_dn = True
        if not has_term:
            up = t.upper()
            if any(k in up or k in t for k in terms):
                has_term = True
        if has_dn and has_term:
            return True
    return False


# ============================================================
# 10. 領域検出実行（_run_region_detection）
# ============================================================

def _run_region_detection(lines, det_cfg, frames, frame_area, frame_labels,
                          connection_points, rotated_edge_roles):
    """lines から H/V 分類 → (図面枠ごとの候補面リスト, 図面枠ごとの行き止まり枝
    リスト) を返す。`analyze_dxf_regions` の3パス検出（LINEのみ→LWPOLYLINE追加→
    横ギャップ橋渡し）が、それぞれこの関数を1回呼んで結果を得る。"""
    RH, RV = _split_axis_aligned(lines, det_cfg['snap'])
    fc = []
    dangling_by_frame = []
    for fi, frame in enumerate(frames):
        cands_list = []
        det_regions, det_dangling = _detect_regions(
            RH, RV, frame, frame_area, det_cfg,
            connection_points, raw_lines=lines)
        for reg in det_regions:
            if det_cfg['exclude_titleblock'] and _is_titleblock_region(reg['polygon'], frame_labels):
                continue
            if det_cfg['exclude_connection_point_regions']:
                cp = _count_connection_points_on_boundary(
                    reg['polygon'], connection_points, det_cfg['connection_point_margin'])
                if cp >= det_cfg['connection_point_threshold']:
                    continue
            ncands, ntiers = region_name_candidates(
                reg['polygon'], frame_labels,
                max_dist=det_cfg['name_max_dist'], min_dist=det_cfg['name_min_dist'],
                min_letters=det_cfg['name_min_letters'],
                rotated_edge_roles=rotated_edge_roles,
                exclude_circuit_symbols=det_cfg['exclude_circuit_symbols'],
                exclude_terms=det_cfg['name_exclude_terms'],
                exclude_lowercase=det_cfg['name_exclude_lowercase'],
                circuit_keep_terms=det_cfg.get('circuit_symbol_keep_terms', ('RACK',)))
            cands_list.append({
                'polygon': reg['polygon'], 'area': reg['area'],
                'name_candidates': ncands,
                'default_name': ncands[0][1] if ncands else '',
                'default_name_tier': ntiers.get(ncands[0][1]) if ncands else None,
                'tier_by_text': ntiers,
            })
        fc.append(cands_list)
        dangling_by_frame.append([d for d in det_dangling if d['entities']])
    return fc, dangling_by_frame


def _remove_overlap_claimed_candidates(regions):
    """重なる領域同士で、同じ名称候補テキストをより近い側（小さい距離）の領域
    だけに残し、遠い側からは取り除く（`regions_overlap()` が True の領域間のみ）。

    `region_name_candidates()` は領域ごとに独立して評価するため、入れ子/重なる
    2領域（例 `EE6313-546-01E.dxf` の外側`B CHAMBER`・内側`BAKE HEATER UNIT RX`）
    では、内側領域の名称ラベルが外側領域の境界からも Tier1/2 の許容距離内に
    収まり、外側領域の候補リストにも内側領域の名称が残ることがある
    （v1.5.13 までの既知の非対称ケース。`test_nested_regions_each_get_own_confident_default_name`
    が文書化）。しかし重なる領域は定義上別の物理領域であり（`regions_overlap` を
    名称同期防止に使っている理由そのもの）、内側領域がその名称をはるかに高い
    確信度（小さい距離）で確定的に持っている以上、外側領域がそれを選択肢として
    提示するのは利用者を誤解させる（ユーザー報告: 領域2が確定済みの名称が
    領域1の候補にも出てくるのは矛盾）。同じテキストについて、重なる領域の中で
    最小距離を持つ領域だけに候補を残し、他の重なる領域からは除去する。
    `default_name`/`default_name_tier` も除去結果に応じて再計算する。
    距離が等しい（明確な優劣がない）場合はどちらからも除去しない。
    """
    n = len(regions)
    original = [r['name_candidates'] for r in regions]  # 比較は変更前のスナップショットで行う
    overlap_cache = {}

    def _overlap(i, j):
        key = (i, j) if i < j else (j, i)
        if key not in overlap_cache:
            overlap_cache[key] = regions_overlap(regions[i]['polygon'], regions[j]['polygon'])
        return overlap_cache[key]

    for i in range(n):
        tier_by_text = regions[i].pop('_tier_by_text', {})
        cands = original[i]
        if not cands:
            continue
        kept = []
        for d, t in cands:
            claimed_by_closer = False
            for j in range(n):
                if j == i or not _overlap(i, j):
                    continue
                if any(t2 == t and d2 < d for d2, t2 in original[j]):
                    claimed_by_closer = True
                    break
            if not claimed_by_closer:
                kept.append((d, t))
        if len(kept) != len(cands):
            regions[i]['name_candidates'] = kept
            regions[i]['default_name'] = kept[0][1] if kept else ''
            regions[i]['default_name_tier'] = (
                tier_by_text.get(kept[0][1]) if kept else None)


# ============================================================
# 10b. 補完面解消（_resolve_complement_faces）
# ============================================================

def _vertex_in_corner_set(vertex, corner_list, tol=1.0):
    """vertex が corner_list の中に許容誤差 tol 以内で一致する点があるか。"""
    vx, vy = vertex
    return any(abs(vx - px) < tol and abs(vy - py) < tol for px, py in corner_list)


def _detect_complement_pairs(regions, tol=1.0):
    """補完面ペア (large_idx, small_idx) のリストを返す。

    補完面とは: small の全コーナー頂点が large のコーナー頂点集合に含まれ、
    large が small より多くのコーナー頂点を持ち、かつ 2 領域が重なる（overlap）
    場合に、large は small の「補完面」と定義する。

    平面グラフ半面探索で境界を共有する2面を生成するとき、共有辺を挟む一方の面が
    「小さい正しい面」、他方が「外側に回り込んだ補完面」として現れる。補完面は
    小さい面より必ず多くの頂点を持ち（追加頂点がある）、小さい面の全頂点を包含する。

    戻り値: [(large_idx, small_idx), ...]
    """
    n = len(regions)
    corners = [r['corners'] for r in regions]
    results = []
    for i in range(n):          # large 候補
        for j in range(n):      # small 候補
            if i == j:
                continue
            ci, cj = corners[i], corners[j]
            if len(ci) <= len(cj):
                continue        # large は small より多くの頂点が必要
            if not all(_vertex_in_corner_set(v, ci, tol) for v in cj):
                continue        # small の全頂点が large の頂点集合に含まれる必要あり
            if not regions_overlap(regions[i]['polygon'], regions[j]['polygon']):
                continue
            results.append((i, j))
    return results


def _extract_complement_subpolygons(large_corners, small_corners, tol=1.0):
    """補完面 large の境界を辿り、small に含まれない追加頂点の連続区間を切り出して
    サブ領域ポリゴン（リスト of リスト[(x,y)]）を返す。

    サブ領域の形状: [attachment_start, extra_v1, ..., extra_vN]
    （attachment_start から extra 頂点列を辿り、attachment_start に直線で戻る閉多角形）
    """
    n = len(large_corners)

    def is_shared(v):
        return _vertex_in_corner_set(v, small_corners, tol)

    flags = [is_shared(v) for v in large_corners]
    subregions = []
    visited_starts = set()
    for i in range(n):
        if flags[i] and not flags[(i + 1) % n]:
            attachment_start = large_corners[i]
            start_idx = (i + 1) % n
            if start_idx in visited_starts:
                continue
            extra_seq = []
            k = start_idx
            while k < n + start_idx:
                cur = large_corners[k % n]
                if is_shared(cur):
                    break
                extra_seq.append(cur)
                k += 1
            if extra_seq:
                visited_starts.add(start_idx)
                subregions.append([attachment_start] + extra_seq)
    return subregions


def _resolve_complement_faces(regions, frame_area, next_id=None):
    """補完面を検出してサブ領域に分割し、補完面を除去した新リストを返す。

    `_remove_overlap_claimed_candidates` より前に呼ぶこと（'_tier_by_text' が
    まだ存在するうちに処理する必要があるため）。

    処理の流れ:
      1. _detect_complement_pairs で大（補完面）・小（基準面）ペアを検出
      2. 補完面の頂点から基準面の頂点を除いた「追加頂点列」でサブ領域ポリゴンを生成
      3. 補完面の名称候補から基準面にクレームされた名称を除き、サブ領域に継承
      4. サブ領域を regions に追加し、補完面を除去して返す
    """
    pairs = _detect_complement_pairs(regions)
    if not pairs:
        return regions

    if next_id is None:
        next_id = max((r['id'] for r in regions), default=-1) + 1

    to_remove = {large_i for large_i, _ in pairs}
    new_regions = [r for i, r in enumerate(regions) if i not in to_remove]

    for large_i, small_i in pairs:
        comp_face = regions[large_i]
        base_face = regions[small_i]

        claimed = {t for _, t in base_face.get('name_candidates', [])}
        comp_tier = comp_face.get('_tier_by_text', {})

        inherited_cands = [(d, t) for d, t in comp_face.get('name_candidates', [])
                           if t not in claimed]
        inherited_tier = {t: comp_tier[t] for _, t in inherited_cands if t in comp_tier}

        default_name = inherited_cands[0][1] if inherited_cands else ''
        default_name_tier = inherited_tier.get(default_name) if default_name else None

        sub_polys = _extract_complement_subpolygons(comp_face['corners'], base_face['corners'])
        for sub_poly in sub_polys:
            sub_area = _polygon_area(sub_poly)
            new_regions.append({
                'id': next_id,
                'frame': comp_face.get('frame', 0),
                'polygon': sub_poly,
                'corners': _polygon_corners(sub_poly),
                'area': sub_area,
                'area_pct': 100.0 * sub_area / frame_area if frame_area > 0 else 0.0,
                'name_candidates': list(inherited_cands),
                'default_name': default_name,
                'default_name_tier': default_name_tier,
                'dangling_edges': [],
                '_tier_by_text': dict(inherited_tier),
            })
            next_id += 1

    return new_regions


def _detect_union_parents(regions, tol=1.0, area_tol=1.0):
    """結合親領域（union parent）の {親インデックス: (子Jインデックス, 子Kインデックス)} を返す。

    横線分または縦線分で 2 分割された兄弟矩形の「合体親」が補完面として誤検出される
    ケース（例: L CHAMBER / FX CHAMBER を横線分で分割した図面で、親矩形が別の領域
    として残る）に対応する。_resolve_complement_faces は頂点数の差（large > small）
    を前提とするため、全領域が 4 頂点の等頂点数ケースは検出できない。

    検出条件（全て満たす）:
      1. area(P) ≈ area(Q) + area(R)  ← P が Q と R の合体サイズ
      2. P の全コーナーが Q.corners ∪ R.corners に含まれる
      3. regions_overlap(P, Q) かつ regions_overlap(P, R)  ← P が Q/R を内包
      4. NOT regions_overlap(Q, R)  ← Q と R は非重複な兄弟

    戻り値: {parent_idx: (child_j_idx, child_k_idx), ...}
    """
    n = len(regions)
    corners = [r['corners'] for r in regions]
    areas = [r['area'] for r in regions]
    result = {}

    for i in range(n):
        if i in result:
            continue
        for j in range(n):
            if j == i:
                continue
            for k in range(j + 1, n):
                if k == i:
                    continue
                # 条件 1: 面積一致
                if abs(areas[i] - areas[j] - areas[k]) > area_tol:
                    continue
                # 条件 2: P の全頂点が Q∪R の頂点集合に含まれる
                if not all(
                    _vertex_in_corner_set(v, corners[j], tol)
                    or _vertex_in_corner_set(v, corners[k], tol)
                    for v in corners[i]
                ):
                    continue
                # 条件 3: P は Q/R を内包（重なる）
                if not regions_overlap(regions[i]['polygon'], regions[j]['polygon']):
                    continue
                if not regions_overlap(regions[i]['polygon'], regions[k]['polygon']):
                    continue
                # 条件 4: Q と R は互いに重ならない
                if regions_overlap(regions[j]['polygon'], regions[k]['polygon']):
                    continue
                result[i] = (j, k)
                break
            if i in result:
                break

    return result


def _force_include_union_children(cands_list, area_tol=1.0, corner_tol=1.0):
    """面積フィルタ未適用の生候補リスト `cands_list` に対して合体親（union parent）を
    検出し、子側候補のインデックス集合を返す（呼び出し側が面積閾値を問わず採用する
    ために使う）。

    通常の採用判定（単独面積>=20%、または同名2ピース合算>=10%）は、兄弟矩形が
    互いに異なる名称を持ち、かつどちらも単独で閾値未満の場合（例:
    `DE5434-563-03A.dxf` の `SB-1A(FX1)`/`CN I/F B.D TYPE3(CN-IF3-1A)`、各7.6-7.7%）
    に対応できない。この2矩形を包む合体面（外側の閉領域。縦ギャップ橋渡しが両側の
    仕切り線を橋渡しした結果生じる、実体のない面積34292.5の見かけ上の領域）が
    偶然どちらか一方の兄弟と同名候補を共有すると、その兄弟だけが同名2ピース合算で
    救済され、もう一方の兄弟は候補にすら残らず消えていた。

    `_detect_union_parents` は面積一致・頂点包含・非重複という強い幾何学的根拠で
    合体関係を判定するため、採用フィルタより前（全ての生候補が揃った時点）で
    適用すれば、面積閾値に関わらず両方の子を正しく拾える。採用後は既存の
    `_resolve_union_parents` が同じ合体親をあらためて検出し、子の名称を除いた
    固有名が無ければ合体親自体を除去する（従来通り）。

    **補完面ペアと競合する三つ組は除外する**: `_detect_complement_pairs`
    （頂点数が非対称な大小2面のペア。`_resolve_complement_faces` が別途処理する）
    に関与する候補が三つ組（親・子2つ）のいずれかに含まれる場合は force-include
    の対象から外す。1つの面が2つの独立した仕組み（頂点差分による補完面カット出し／
    面積一致による合体親分解）に同時に「取り合われる」と、同じ物理領域が異なる
    形（カット出し後のサブポリゴン／生の面そのもの）で二重に採用されてしまう
    （実例: `EE6313-545-01D.dxf` の B CHAMBER/FX CHAMBER。補完面〔78.2%〕は
    B CHAMBER〔63.64%、単独で閾値超のため本来 force-include 不要〕+ 小面
    〔14.59%〕の合体としても幾何学的に成立してしまい、後者が
    `_resolve_complement_faces` によるカット出し結果と別に、生の面のまま
    二重採用され `B CHAMBER` が2件検出されるようになっていた。B CHAMBER は
    既にこの補完面ペアの「小」側であるため、複合面パターンとして除外される）。
    """
    if len(cands_list) < 3:
        return set()
    enriched = [dict(cf, corners=_polygon_corners(cf['polygon'])) for cf in cands_list]
    complement_involved = set()
    for large_i, small_i in _detect_complement_pairs(enriched):
        complement_involved.add(large_i)
        complement_involved.add(small_i)
    parent_to_children = _detect_union_parents(enriched, tol=corner_tol, area_tol=area_tol)
    child_idx = set()
    for i, (j, k) in parent_to_children.items():
        if i in complement_involved or j in complement_involved or k in complement_involved:
            continue
        child_idx.add(j)
        child_idx.add(k)
    return child_idx


def _name_union_parent(parent_region, child_regions, labels, cfg,
                        exclude_names=None):
    """合体親領域の名称候補を、子領域が未採用のラベルから探索して返す。

    通常の `region_name_candidates` と異なる点:
      - require_inside を緩和し、領域の外側（底辺の下方向）も探索対象にする
        （合体親の名称ラベルが底辺のすぐ外側に置かれることがある。
         例: DE5434-563-03A.dxf の 'FX CHAMBER' @ y=76.4 は polygon 外）
      - 子領域がすでに採用した候補テキストを除外する
      - exclude_names に含まれるテキストを除外する（他の非子領域が使用中の名称）
      - 底辺中央 x 座標への近接度（中心距離）を距離の第2ソートキーにし、
        同距離の候補が複数あるとき中央により近いラベルを優先する

    戻り値: (name_candidates, tier_by_text) のタプル。
      name_candidates: [(distance, text), ...]
      tier_by_text: {text: 1}  （底辺探索由来 → Tier1 相当とみなす）
    """
    polygon = parent_region['polygon']
    max_dist = cfg.get('name_max_dist', 10.0)
    min_dist = cfg.get('name_min_dist', 1.0)
    min_letters = cfg.get('name_min_letters', 3)
    exclude_terms = cfg.get('name_exclude_terms', ('NOTE', '☆'))
    exclude_lowercase = cfg.get('name_exclude_lowercase', True)
    exclude_circuit_symbols = cfg.get('exclude_circuit_symbols', True)
    circuit_keep_terms = cfg.get('circuit_symbol_keep_terms', ('RACK',))

    # 子領域が採用済みのテキスト + 他の非子領域が使用中の名称 を除外対象として収集
    claimed = set(exclude_names or ())
    for child in child_regions:
        for _, t in child.get('name_candidates', []):
            claimed.add(t)

    # 底辺エッジとその x 全体中央を算出
    bottom = _bottom_edges(polygon)
    if not bottom:
        return [], {}
    all_x0 = min(seg[0] for seg in bottom)
    all_x1 = max(seg[1] for seg in bottom)
    center_x = (all_x0 + all_x1) / 2.0

    # 底辺エッジへの距離（領域外も許容）と中央距離でスコアリング
    scored = []
    for (t, x, y) in labels:
        if not _is_valid_name_candidate(t, min_letters, exclude_lowercase,
                                        exclude_terms, exclude_circuit_symbols,
                                        circuit_keep_terms) or t in claimed:
            continue
        dist = _dist_to_bottom_edge((x, y), bottom)
        if dist < min_dist or dist > max_dist:
            continue
        centrality = abs(x - center_x)
        scored.append((dist, centrality, t))

    scored.sort(key=lambda c: (c[0], c[1]))
    seen = set()
    out = []
    tier_by_text = {}
    for dist, _centrality, t in scored:
        if t in seen:
            continue
        seen.add(t)
        out.append((round(dist, 1), t))
        tier_by_text[t] = 1  # 底辺探索由来 → Tier1 相当
        if len(out) >= 8:
            break
    return out, tier_by_text


def _resolve_union_parents(regions, labels=None, cfg=None):
    """結合親領域（2兄弟矩形の合体）を検出し、名称を再探索した上でリストを返す。

    子領域が採用済みの名称候補を除外し、底辺中央近接条件を加味して
    合体親固有の名称ラベルを探索する（`_name_union_parent` 参照）。
    未採用ラベルが見つかった場合は親を残して名称を更新する。
    見つからなかった場合は従来通り除去する。
    `labels` が与えられない場合は全ての結合親を除去する（後方互換）。

    `_resolve_complement_faces` の呼び出し後（補完面除去済みの状態）に呼ぶ。
    """
    parent_to_children = _detect_union_parents(regions)
    if not parent_to_children:
        return regions

    to_remove = set()
    if labels is not None:
        effective_cfg = cfg or {}
        parent_indices = set(parent_to_children.keys())
        child_indices = {c for cs in parent_to_children.values() for c in cs}
        # フレーム別に「既使用名称」を管理（異なるフレームは独立して同名を許可）
        parent_claimed_by_frame = defaultdict(set)
        for parent_idx, (child_j, child_k) in parent_to_children.items():
            parent = regions[parent_idx]
            parent_frame = parent.get('frame', 0)
            children = [regions[child_j], regions[child_k]]
            # 同一フレーム内の非親・非子領域が使用中の名称を除外対象とする
            same_frame_names = {
                regions[i]['default_name']
                for i in range(len(regions))
                if i not in parent_indices and i not in child_indices
                and regions[i].get('default_name')
                and regions[i].get('frame', 0) == parent_frame
            } | parent_claimed_by_frame[parent_frame]
            new_cands, new_tiers = _name_union_parent(
                parent, children, labels, effective_cfg,
                exclude_names=same_frame_names)
            if new_cands:
                # 未採用ラベルが見つかった → 親を残して名称を更新
                parent['name_candidates'] = new_cands
                parent['_tier_by_text'] = new_tiers
                parent['default_name'] = new_cands[0][1]
                parent['default_name_tier'] = new_tiers.get(new_cands[0][1])
                parent_claimed_by_frame[parent_frame].add(new_cands[0][1])
            else:
                # 未採用ラベルがない → 従来通り除去
                to_remove.add(parent_idx)
    else:
        to_remove = set(parent_to_children.keys())

    return [r for i, r in enumerate(regions) if i not in to_remove]


# ============================================================
# 11. トップレベル解析（公開API）
# ============================================================

def analyze_dxf_regions(dxf_file: str, config: dict | None = None) -> dict:
    """DXFファイルを解析し、図面枠・閉領域（名称候補つき）・図面枠内ラベルを返す。

    戻り値 dict:
      frames: [(xl,xr,y0,y1), ...]
      frame_area: float
      labels: [(text, x, y), ...]  （図面枠内のみ）
      regions: [{id, frame, polygon, area, area_pct, name_candidates, default_name,
                 default_name_tier, dangling_edges}]
        default_name_tier: default_name（name_candidates[0]）の優先順位（1/2/3、
          候補が無い場合は None）。`region_name_candidates()` の Tier（下端/上端
          最近傍=1/2、境界全体への距離フォールバック=3）と同義。`app.py` の
          他領域への選択伝播（同期）が、確信度の高い自前の候補（Tier1/2）を
          確信度の低い他領域の選択で上書きしないようにするために使う（v1.5.9）。
        dangling_edges（領域ごと）: [{attachment, entities: [{handle, start, end}, ...]}]
          この領域の境界探索から除外された行き止まり枝（次数1のノードに繋がり、
          どこにも閉じていない境界線分の連結成分）。半面探索がこの枝を折り返す
          ために生じる「同じ頂点が2回連続する」アーティファクトの原因になっていた
          箇所を、面探索前に除去している（v1.5.7）。`attachment` は枝が現存する
          境界グラフに取り付く座標（この領域のポリゴン境界上に乗る）。1本の枝が
          複数の生エンティティ（短い線分の連なり、または閉領域の境界として一部
          だけ使われている長い線の余剰部分）で構成される場合は `entities` に
          複数件入る。取り付け点がどの領域の境界にも乗らない枝（無関係な部品等）
          は報告されない。
      error: str | None
    """
    cfg = dict(DEFAULT_REGION_CONFIG)
    if config:
        cfg.update(config)
    result = {'frames': [], 'frame_area': 0.0, 'labels': [], 'regions': [], 'error': None}
    try:
        doc = ezdxf.readfile(dxf_file)
        msp = doc.modelspace()
        frame_lines, region_lines, region_lines_lp, label_entities, connection_points = \
            _collect_region_geometry(msp, cfg)

        frames = detect_drawing_frames(frame_lines, cfg['snap'])
        result['frames'] = frames
        if not frames:
            result['error'] = ('図面枠（太さ %d の線で囲まれた枠）が見つかりませんでした。'
                               % cfg['frame_lineweight'])
            return result
        frame_area = (frames[0][1] - frames[0][0]) * (frames[0][3] - frames[0][2])
        result['frame_area'] = frame_area

        # 図面枠内ラベル（重複除去）
        seen = set()
        frame_labels = []
        for it in label_entities:
            _, clean_text, (x, y) = extract_text_from_entity(it)
            if not clean_text:
                continue
            in_frame = any(xl - 1 <= x <= xr + 1 and y0 - 1 <= y <= y1 + 1
                           for (xl, xr, y0, y1) in frames)
            if not in_frame:
                continue
            key = (clean_text, round(x, 1), round(y, 1))
            if key in seen:
                continue
            seen.add(key)
            frame_labels.append((clean_text, x, y))
        result['labels'] = frame_labels

        single_thr = frame_area * cfg['area_ratio']            # 単独領域の閾値(20%)
        group_thr = frame_area * cfg.get('group_area_ratio', 0.10)  # 同名複数ピース合算の閾値(10%)
        rotated = _is_globally_rotated(label_entities)
        rotated_edge_roles = _rotated_edge_roles(label_entities) if rotated else None

        # 1) LINE のみで領域検出を試みる
        lines_for_detection = region_lines
        frame_cands, dangling_by_frame = _run_region_detection(
            lines_for_detection, cfg, frames, frame_area, frame_labels,
            connection_points, rotated_edge_roles)

        def _zero_hit_frame_indices(cands):
            return [fi for fi, cl in enumerate(cands)
                    if not any(cf['area'] >= single_thr for cf in cl)]

        # LINE だけで閾値超え候補がゼロだった図面枠に限り、LWPOLYLINE を追加して
        # 再検出する（例: EE6888-631-01A.dxf など境界が LWPOLYLINE で描かれた図面
        # への対応）。判定・再検出とも図面枠単位（zero_fis のみ）で行う——ファイル
        # 全体で1件でも候補があれば以降のパスを丸ごとスキップする実装だと、同じ
        # ファイル内の「別の図面枠がたまたま先に見つかる」だけで、本来
        # LWPOLYLINE/回転対応が必要な図面枠が永久に救済されないバグがあった
        # （`DE5434-553-10B.dxf` の `CONTROL BOX CORE FX`/`RX` で確認。area_ratio を
        # 10%→15% に変えるだけで検出有無が反転するという閾値依存の非単調な挙動が
        # 発覚の手がかりだった。2026-07-12 修正）。
        # LINE でも十分な候補がある図面枠には LWPOLYLINE を追加しない
        # （小部品の LWPOLYLINE が境界線の corner-partner 判定を誤らせるため）。
        zero_fis = _zero_hit_frame_indices(frame_cands)
        if zero_fis and region_lines_lp:
            lines_for_detection = region_lines + region_lines_lp
            sub_frames = [frames[fi] for fi in zero_fis]
            fc2, dg2 = _run_region_detection(
                lines_for_detection, cfg, sub_frames, frame_area, frame_labels,
                connection_points, rotated_edge_roles)
            for j, fi in enumerate(zero_fis):
                frame_cands[fi] = fc2[j]
                dangling_by_frame[fi] = dg2[j]

        # それでも閾値超え候補がゼロだった図面枠に限り、かつラベルの過半数が90°回転
        # している（=図面全体が90°回転して描かれている）場合のみ、横線分のギャップ
        # 橋渡しを有効にして再検出する（安全条件＝縦線分の端点とのコーナー一致無し・
        # CIRCLE無し、は橋渡し縦線分と同じ）。判定・再検出とも図面枠単位（zero_fis の
        # み）で行う理由は上記 LWPOLYLINE パスと同じ（1ファイル内の一部の図面枠だけが
        # 回転コンテンツを持つケースを取りこぼさない）。
        # 回転判定（`rotated`）はファイル全体のラベル集計に基づく既存の判定のまま
        # 維持する——通常向きの図面枠で「単に検出ゼロ件だったから」をトリガーに横線分
        # も橋渡ししてしまうと、無関係な隣接矩形を誤って結合する副作用があるため
        # （`_is_globally_rotated` 参照）。
        det_cfg = cfg
        zero_fis = _zero_hit_frame_indices(frame_cands)
        if zero_fis and rotated:
            det_cfg = dict(cfg)
            det_cfg['bridge_horizontal_gaps'] = True
            sub_frames = [frames[fi] for fi in zero_fis]
            fc3, dg3 = _run_region_detection(
                lines_for_detection, det_cfg, sub_frames, frame_area, frame_labels,
                connection_points, rotated_edge_roles)
            for j, fi in enumerate(zero_fis):
                frame_cands[fi] = fc3[j]
                dangling_by_frame[fi] = dg3[j]

        # 4パス目（レベル汚染フォールバック・図面枠単位）: 領域境界線と同属性の無関係な
        # 近接線分（例: 境界線 y=122.00 の 0.37 上のコネクタ箱底辺 y=122.37）が共線結合の
        # レベルクラスタ平均を汚染して境界線をシフトさせ、縦線端点との接続が切れて閉領域が
        # 不成立になる枠がある（EE6892-039-05B.dxf の2枠目、SYSTEM I/F BOX）。
        # そのような「閾値超え候補ゼロの枠」に限り、スパン単位レベル（汚染なし）で再検出する。
        # 発動条件（明示的な信号。「ゼロ件だから試す」だけをトリガーにしない）:
        #   (a) 閾値超えゼロの枠がある
        #   (b) 他の枠には閾値超えの領域がある（=全枠ゼロの図面タイプ〔電源基板の回路図等〕
        #       では発動しない。EE6333-610-07A 等で基板外形を誤検出するのを防ぐ）
        # 採用条件: 回復した領域の名称が他枠で検出済みの名称と一致する枠のみ置き換える
        #   （1ファイル複数図面は同じユニット群の続きを描くため、同名領域が枠をまたいで
        #    現れることを回復の根拠とする）。
        zero_fis = [fi for fi, cl in enumerate(frame_cands)
                    if not any(cf['area'] >= single_thr for cf in cl)]
        hit_names = {cf['default_name']
                     for fi, cl in enumerate(frame_cands) if fi not in zero_fis
                     for cf in cl
                     if cf['area'] >= single_thr and cf['default_name']}
        if zero_fis and hit_names:
            cfg_span = dict(det_cfg)
            cfg_span['span_level_merge'] = True
            sub_frames = [frames[fi] for fi in zero_fis]
            fc2, dg2 = _run_region_detection(
                lines_for_detection, cfg_span, sub_frames, frame_area, frame_labels,
                connection_points, rotated_edge_roles)
            for j, fi in enumerate(zero_fis):
                if any(cf['area'] >= single_thr and cf['default_name'] in hit_names
                       for cf in fc2[j]):
                    frame_cands[fi] = fc2[j]
                    dangling_by_frame[fi] = dg2[j]

        # 2) 第1図面（最左フレーム）で「同名複数ピース合算>=group_thr」となる名称を
        #    ターゲットとする（MPD RACK2 のように2矩形で合算が閾値超のケース）。
        #    他図面では、このターゲット名称の矩形を面積に関係なく抽出する。
        target_names = set()
        if frame_cands:
            by_name = defaultdict(list)
            for cf in frame_cands[0]:
                if cf['default_name']:
                    by_name[cf['default_name']].append(cf['area'])
            for nm, areas in by_name.items():
                if len(areas) >= 2 and sum(areas) >= group_thr:
                    target_names.add(nm)

        # 3) 採用条件: 個別面積>=単独閾値(20%)、または 名称がターゲット（複数ピース合算で
        #    第1図面が閾値超）、または合体親（union parent）の子と確認された候補
        #    （`_force_include_union_children`、面積閾値を問わず採用。後述）。
        #    行き止まり枝は、その取り付け点(attachment)が当該領域のポリゴン境界上に
        #    乗るものだけを、その領域の `dangling_edges` として絞り込む（無関係な
        #    部品・他領域の枝を混在させない）。
        attach_tol = max(cfg.get('face_snap', 0.1), 0.5)
        regions = []
        rid = 0
        for fi, cands_list in enumerate(frame_cands):
            frame_dangling = dangling_by_frame[fi] if fi < len(dangling_by_frame) else []
            force_idx = _force_include_union_children(cands_list)
            for cidx, cf in enumerate(cands_list):
                if not (cf['area'] >= single_thr
                        or (cf['default_name'] and cf['default_name'] in target_names)
                        or cidx in force_idx):
                    continue
                region_dangling = [
                    br for br in frame_dangling
                    if br['attachment'] is not None
                    and _dist_point_to_polygon(br['attachment'], cf['polygon']) <= attach_tol
                ]
                regions.append({
                    'id': rid,
                    'frame': fi,
                    'polygon': cf['polygon'],
                    'corners': _polygon_corners(cf['polygon']),
                    'area': cf['area'],
                    'area_pct': 100.0 * cf['area'] / frame_area,
                    'name_candidates': cf['name_candidates'],
                    'default_name': cf['default_name'],
                    'default_name_tier': cf.get('default_name_tier'),
                    'dangling_edges': region_dangling,
                    '_tier_by_text': cf.get('tier_by_text', {}),
                })
                rid += 1
        regions = _resolve_complement_faces(regions, frame_area, next_id=rid)
        regions = _resolve_union_parents(regions, labels=frame_labels, cfg=cfg)
        _remove_overlap_claimed_candidates(regions)
        result['regions'] = regions

        del doc, msp
        gc.collect()
    except Exception as e:
        result['error'] = str(e)
        gc.collect()
    return result


def assign_region_labels(
    labels: list[tuple[str, float, float]],
    named_regions: list[dict],
) -> list[tuple[str, float, float, list[str]]]:
    """各ラベル(text,x,y)が内包される領域名のリストを返す。

    named_regions: [{'polygon': [...], 'name': str}]（名称確定済み）。
    戻り値: [(text, x, y, [region_name, ...]), ...]
    """
    out = []
    for (t, x, y) in labels:
        names = []
        for reg in named_regions:
            nm = reg.get('name')
            if nm and _point_in_polygon((x, y), reg['polygon']) and nm not in names:
                names.append(nm)
        out.append((t, x, y, names))
    return out


def build_region_results(
    analyses: dict,
    name_selections: dict,
    sort_value: str,
    filter_circuit_only: bool = False,
) -> dict:
    """解析結果とユーザーがチェックした名称から、ファイルごとの領域付きラベル集計を構築する。

    name_selections: {(filename, region_id): [チェックされた名称, ...]}
    filter_circuit_only: True のとき機器符号のみを対象とする（非機器符号を除外）。

    出力（集計・記録）の領域名・ラベルは `normalize_width()` で半角へ統一する。
    図面上の表記が半角/全角どちらでも同じ語は同じ行に集計され、全角の機器符号も
    正規化後に機器符号フィルタで正しく判定される。
    """
    region_results = {}
    for fname, analysis in analyses.items():
        named = []
        named_region_ids = set()
        no_name_idx = 0
        for reg in analysis.get('regions', []):
            chosen_names = name_selections.get((fname, reg['id']), [])
            if not chosen_names and not reg.get('name_candidates'):
                no_name_idx += 1
                chosen_names = [f"no name {no_name_idx}"]
            for nm in chosen_names:
                if not nm:
                    continue
                named.append({
                    'polygon': reg['polygon'], 'name': normalize_width(nm),
                    'id': reg['id'], 'frame': reg['frame'], 'area_pct': reg['area_pct'],
                })
                named_region_ids.add(reg['id'])

        all_labels = [(normalize_width(t), x, y)
                      for (t, x, y) in analysis.get('labels', [])]

        if filter_circuit_only:
            texts = [l[0] for l in all_labels]
            matched, _ = filter_non_circuit_symbols(texts)
            matched_set = set(matched)
            labels = [l for l in all_labels if l[0] in matched_set]
            filtered_count = len(all_labels) - len(labels)
        else:
            labels = all_labels
            filtered_count = 0

        assigned = assign_region_labels(labels, named)

        cnt = Counter()
        region_of = defaultdict(set)
        in_region_count = 0
        label_count_per_region = defaultdict(int)
        region_label_counts = defaultdict(Counter)
        for (text, x, y, names) in assigned:
            cnt[text] += 1
            if names:
                in_region_count += 1
            for n in names:
                region_of[text].add(n)
                label_count_per_region[n] += 1
                region_label_counts[n][text] += 1

        rows = [
            {'ラベル': t, '個数': cnt[t], '領域': ', '.join(sorted(region_of[t]))}
            for t in cnt
        ]
        if sort_value == 'asc':
            rows.sort(key=lambda r: r['ラベル'])
        elif sort_value == 'desc':
            rows.sort(key=lambda r: r['ラベル'], reverse=True)

        for r in named:
            r['label_count'] = label_count_per_region.get(r['name'], 0)

        region_results[fname] = {
            'rows': rows,
            'named': named,
            'frames': len(analysis.get('frames', [])),
            'regions_detected': len(analysis.get('regions', [])),
            'regions_named': len(named_region_ids),
            'total_in_frame': len(all_labels),
            'filtered_count': filtered_count,
            'final_count': len(labels),
            'in_region_count': in_region_count,
            'drawing_number': analysis.get('main_drawing_number') or '',
            'region_label_counts': {n: dict(c) for n, c in region_label_counts.items()},
        }
    return region_results


def build_region_label_summary(region_results: dict) -> tuple[list[tuple[str, str]], list[dict]]:
    """`build_region_results()` の結果から、領域名ごとに全ファイル横断でラベルを
    集計した「領域別ラベル一覧」用の行データを構築する。

    領域名は同名であれば複数ファイルにまたがって1グループに合算される
    （矩形領域抽出は元々「同名複数ピース合算」「他図面でも同名採用」等、複数
    ファイルで同じ領域名を共有する前提の設計であるため、これは自然な拡張）。

    戻り値: (ファイル一覧 [(fname, 表示用図番), ...]（ファイル出現順。図番未抽出
    時はファイル名〔拡張子なし〕にフォールバック）, 行データのリスト)。行データは
    {'領域名': str, 'ラベル': str, '合計個数': int, 'per_file': {fname: 個数}}
    （`per_file` は表示用図番ではなく **fname をキー**にする——2ファイルが同じ図番
    〔未抽出時のフォールバックを含む〕を持つ場合の値衝突を避けるため）。
    領域名は昇順、各領域内のラベルも昇順に整列する。ファイルにそのラベルが
    存在しない場合の個数は 0（`per_file` に未登場のキーとして表現、呼び出し側で
    `.get(fname, 0)` する）。
    """
    files = []
    for fname, data in region_results.items():
        ident = data.get('drawing_number') or os.path.splitext(fname)[0]
        files.append((fname, ident))

    by_region = defaultdict(lambda: defaultdict(int))
    per_file_by_region = defaultdict(lambda: defaultdict(dict))
    for fname, data in region_results.items():
        for region_name, label_counts in data.get('region_label_counts', {}).items():
            for label, count in label_counts.items():
                by_region[region_name][label] += count
                per_file_by_region[region_name][label][fname] = count

    rows = []
    for region_name in sorted(by_region.keys()):
        for label in sorted(by_region[region_name].keys()):
            rows.append({
                '領域名': region_name,
                'ラベル': label,
                '合計個数': by_region[region_name][label],
                'per_file': per_file_by_region[region_name][label],
            })
    return files, rows
