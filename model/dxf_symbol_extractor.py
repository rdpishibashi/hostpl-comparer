"""DXFファイルからの機器符号抽出（複数ファイル→図番ごとのCounter）。

図番はDXFデータから抽出せず、ファイル名（拡張子を除いた部分）をそのまま使う
（確定設計: DXFの表題ブロック解析・図番判別ロジックには依存しない）。

機器符号候補の抽出自体は `model/ref_designator.py`（DXF-extract-labelsから
バイト一致で複製したprimary）の `extract_ref_designator_data()` を使う。
同関数は「確定（confirmed_labels）」「未確定＝人手レビュー対象
（review_labels）」の2集合を返すが、本プロジェクトでは人手レビューUIを
設けず、**両方を機器符号として採用する**（比較ツールでは抽出漏れによる
「ULKESのみ」の誤警報の方が、過剰抽出のノイズより有害なため。2026-09-08、
ユーザー承認済み）。
"""
import os
from collections import Counter

from . import ref_designator


def drawing_number_from_filename(filename):
    """ファイル名（パスでも可）から拡張子を除いた部分を図番として返す。"""
    return os.path.splitext(os.path.basename(filename))[0]


def extract_symbols_from_dxf_file(dxf_path, original_filename=None):
    """1つのDXFファイルから機器符号（候補）を抽出する。

    Args:
        dxf_path: 実際に読み込むDXFファイルのパス（一時ファイルでもよい）
        original_filename: 図番の決定・表示に使うファイル名。省略時は dxf_path を使う

    Returns:
        dict:
            drawing_number: str（ファイル名から決定）
            filename: str（表示用の元ファイル名）
            counter: Counter[str, int]（確定＋未確定を合算したラベル別出現数）
            unconfirmed_labels: set[str]（未確定（review）由来のラベル。
                プレビューで印を付けるために使う）
            warning: str | None（図面枠が検出できずフォールバックした場合の警告）
    """
    display_name = original_filename or dxf_path
    drawing_number = drawing_number_from_filename(display_name)

    data = ref_designator.extract_ref_designator_data(
        dxf_path, original_filename=display_name
    )

    confirmed = [label for label, _x, _y in data['confirmed_labels']]
    review = [label for label, _x, _y in data['review_labels']]

    counter = Counter(confirmed)
    counter.update(review)

    return {
        'drawing_number': drawing_number,
        'filename': display_name,
        'counter': counter,
        'unconfirmed_labels': set(review),
        'warning': data['warning'],
    }


def build_dxf_symbol_map(per_file_results):
    """複数DXFファイルの抽出結果を図番ごとに束ねる。

    Args:
        per_file_results: `extract_symbols_from_dxf_file()` の戻り値のリスト

    Returns:
        tuple[dict[str, Counter], dict[str, set], list[str]]:
            - 図番ごとのCounter（同一図番のファイルが複数あれば合算）
            - 図番ごとの未確定ラベル集合の和
            - 警告メッセージのリスト（図番の重複・図面枠検出フォールバック）
    """
    symbol_map = {}
    unconfirmed_map = {}
    warnings = []

    for result in per_file_results:
        dn = result['drawing_number']

        if result['warning']:
            warnings.append(f"{result['filename']}: {result['warning']}")

        if dn in symbol_map:
            warnings.append(
                f"図番 '{dn}' のDXFファイルが複数あります"
                f"（'{result['filename']}' の内容は既存の抽出結果に合算します）"
            )
            symbol_map[dn].update(result['counter'])
            unconfirmed_map[dn] |= result['unconfirmed_labels']
        else:
            symbol_map[dn] = Counter(result['counter'])
            unconfirmed_map[dn] = set(result['unconfirmed_labels'])

    return symbol_map, unconfirmed_map, warnings
