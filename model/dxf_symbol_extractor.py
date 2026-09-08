"""DXFファイルからの機器符号抽出（複数ファイル→図番ごとのCounter）。

図番はDXFデータから抽出せず、ファイル名（拡張子を除いた部分）をそのまま使う
（確定設計: DXFの表題ブロック解析・図番判別ロジックには依存しない）。

機器符号候補の抽出自体は `model/ref_designator.py`（DXF-extract-labelsから
バイト一致で複製したprimary）の公開関数を個別に組み合わせて行う（**同ファイル
自体は編集しない・バイト一致を維持する**）。`extract_ref_designator_data()`
という一括呼び出し用の便利関数は使わない——同関数は候補パターンに一致した
ラベルのみを返し、非候補（`rejected_labels`）を捨ててしまうため、ULKES側
プレフィックスによる救済（`compare_symbols.rescue_by_ulkes_prefix()`、
2026-09-08導入）に必要な「非候補ラベル一覧」を保持できない。

`_collect_all_labels_fallback()` は `ref_designator.py` の非公開関数
（先頭アンダースコア）に依存している。primary側でリネーム・削除されると
ここが黙って壊れるため、`tests/unit/test_dxf_symbol_extractor.py` に
存在確認のテストを置いている——変更する場合はそちらも確認すること。
"""
import os
from collections import Counter

from . import ref_designator


def drawing_number_from_filename(filename):
    """ファイル名（パスでも可）から拡張子を除いた部分を図番として返す。"""
    return os.path.splitext(os.path.basename(filename))[0]


def extract_symbols_from_dxf_file(dxf_path, original_filename=None):
    """1つのDXFファイルから機器符号候補・非候補ラベルを抽出する。

    `ref_designator.py`の機器符号（候補）抽出パイプライン（図面枠検出→
    フォーマットブロック除外→NFKC正規化）はそのまま適用し、候補パターン判定
    （`is_ref_designator_candidate()`）に一致したものを`counter`、
    一致しなかったものを`rejected_labels`に分ける。

    Args:
        dxf_path: 実際に読み込むDXFファイルのパス（一時ファイルでもよい）
        original_filename: 図番の決定・表示に使うファイル名。省略時は dxf_path を使う

    Returns:
        dict:
            drawing_number: str（ファイル名から決定）
            filename: str（表示用の元ファイル名）
            counter: Counter[str, int]（機器符号候補パターンに一致したラベル）
            rejected_labels: Counter[str, int]（候補パターンに一致しなかった
                ラベル。ULKES側プレフィックスによる救済にのみ使う内部データで、
                プレビューには出さない）
            warning: str | None（図面枠が検出できずフォールバックした場合の警告）
    """
    display_name = original_filename or dxf_path
    drawing_number = drawing_number_from_filename(display_name)

    collected = ref_designator.collect_in_frame_labels(dxf_path)
    if collected['error']:
        warning = collected['error'] + '（図面枠内フィルタなしで全ラベルを対象にします）'
        raw_labels = ref_designator._collect_all_labels_fallback(dxf_path)
    else:
        warning = None
        raw_labels = collected['labels']

    normalized = ref_designator.normalize_labels(raw_labels)

    counter = Counter()
    rejected_labels = Counter()
    for label, _x, _y in normalized:
        if ref_designator.is_ref_designator_candidate(label):
            counter[label] += 1
        else:
            rejected_labels[label] += 1

    return {
        'drawing_number': drawing_number,
        'filename': display_name,
        'counter': counter,
        'rejected_labels': rejected_labels,
        'warning': warning,
    }


def build_dxf_symbol_map(per_file_results):
    """複数DXFファイルの抽出結果を図番ごとに束ねる。

    同一図番のDXFファイルが複数ある場合は**合算せず、最初の1件を採用**する
    （2026-09-08、要求8。ULKES側`build_ulkes_symbol_map()`と同じ「先勝ち」方式に
    揃えた）。採用しなかったファイルの内容（`counter`・`rejected_labels`）は
    一切使わない。

    Args:
        per_file_results: `extract_symbols_from_dxf_file()` の戻り値のリスト
            （アップロード順）

    Returns:
        tuple[dict[str, Counter], dict[str, Counter], list[str]]:
            - 図番ごとの機器符号候補Counter
            - 図番ごとの非候補ラベルCounter（救済判定にのみ使う）
            - 警告メッセージのリスト（図番の重複・図面枠検出フォールバック）
    """
    symbol_map = {}
    rejected_map = {}
    warnings = []

    for result in per_file_results:
        dn = result['drawing_number']

        if result['warning']:
            warnings.append(f"{result['filename']}: {result['warning']}")

        if dn in symbol_map:
            warnings.append(
                f"図番 '{dn}' のDXFファイルが複数あります"
                f"（'{result['filename']}' は無視され、最初に処理したファイルの内容を使用します）"
            )
            continue

        symbol_map[dn] = Counter(result['counter'])
        rejected_map[dn] = Counter(result['rejected_labels'])

    return symbol_map, rejected_map, warnings
