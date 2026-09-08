"""図面(DXF)側とULKES側の機器符号を、図番をキーに比較するコアロジック
（Streamlit非依存の純関数）。

ULKES側は `extract_symbols.extract_circuit_symbols()` が返す展開済み記号
（`_` 分解済み、構成数との過不足補完済み）を使う。補完・超過で付与される
`?` を含む記号は、以下のルールで比較に組み込む（HostPL-extractor/TECHNICAL.md
の「不足分の記号形式」節に準拠、`?` の直後が3桁の数字かどうかで機械的に判別）:

- 末尾 `?`（超過マーク。直後に3桁数字が続かない）: `?` を除去し、本体の
  符号として符号単位比較に使う（例: `CB018A?` → `CB018A`）。
- `{prefix}?{ddd}`（構成数不足の補完。直後に3桁数字が続く）: 特定の符号には
  帰属できないため、符号単位比較には使わず、`prefix` のアルファベット部分に
  ひもづくプレフィックス単位の合計比較に使う（例: `CNCB?001` → プレフィックス
  `CNCB` のULKES側合計に+1）。
"""
import re
from collections import Counter

import pandas as pd

from model.extract_symbols import extract_alphabetic_part

DIFF_COLUMNS = ['符号', '区分', '図面個数', 'ULKES個数']
PREFIX_COLUMNS = ['プレフィックス', '図面合計', 'ULKES合計']

KUBUN_BOTH = '両方'
KUBUN_DXF_ONLY = '図面のみ'
KUBUN_ULKES_ONLY = 'ULKESのみ'

# 表示色（Excel出力・画面表示で共通利用）。DXF-label-compare と同じ配色を採用する
# （2026-08-31、ユーザー指定: 青=図面のみ／緑=ULKESのみ／黄=両方だが個数不一致）。
ROW_STYLE_DXF_ONLY = 'DXF_ONLY'
ROW_STYLE_ULKES_ONLY = 'ULKES_ONLY'
ROW_STYLE_MISMATCH = 'MISMATCH'
ROW_STYLE_MATCH = 'MATCH'

COLOR_DXF_ONLY = {'bg_color': '#D9E1F2', 'font_color': '#1F4E79'}     # 青
COLOR_ULKES_ONLY = {'bg_color': '#C6EFCE', 'font_color': '#006100'}   # 緑
COLOR_MISMATCH = {'bg_color': '#FFEB9C', 'font_color': '#9C6500'}     # 黄

ROW_STYLE_COLORS = {
    ROW_STYLE_DXF_ONLY: COLOR_DXF_ONLY,
    ROW_STYLE_ULKES_ONLY: COLOR_ULKES_ONLY,
    ROW_STYLE_MISMATCH: COLOR_MISMATCH,
    ROW_STYLE_MATCH: None,
}

_COMPLETION_PATTERN = re.compile(r'^(.*)\?(\d{3})$')


def normalize_label(s: str) -> str:
    """全角ASCII(U+FF01-FF5E)を半角に、全角スペースを半角スペースに変換する。
    日本語文字（かな・カナ・漢字）は変換しない（DXF-label-compare と同じ方針。
    `unicodedata.normalize('NFKC', ...)` は使わない——半角カナ→全角カナ変換等の
    副作用があるため）。"""
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif o == 0x3000:
            out.append(' ')
        else:
            out.append(ch)
    return ''.join(out)


def classify_ulkes_symbol(symbol: str) -> tuple:
    """ULKES側の展開済み記号1件を分類する（正規化後の文字列を渡すこと）。

    戻り値: (kind, key)
      kind == 'symbol': key は本体の機器符号（末尾の超過マーク`?`があれば除去済み）。
                         符号単位比較の対象。
      kind == 'prefix': key はプレフィックス（構成数超過補完 `{prefix}?{ddd}` の
                         prefix部分）。個々の符号には帰属できないため、
                         プレフィックス単位の合計比較の対象。
    """
    m = _COMPLETION_PATTERN.match(symbol)
    if m:
        return 'prefix', m.group(1)
    if symbol.endswith('?'):
        return 'symbol', symbol[:-1]
    return 'symbol', symbol


def classify_symbols(symbols) -> tuple:
    """ULKES側の展開済み記号リスト（未正規化）を、符号単位Counterと
    プレフィックス単位Counterに分ける。正規化はここで行う。"""
    symbol_counter = Counter()
    prefix_counter = Counter()
    for raw in symbols:
        kind, key = classify_ulkes_symbol(normalize_label(str(raw)))
        if kind == 'symbol':
            symbol_counter[key] += 1
        else:
            prefix_counter[key] += 1
    return symbol_counter, prefix_counter


def row_style(kubun: str, a_count, b_count) -> str:
    """区分と個数から表示スタイル区分（ROW_STYLE_*）を返す。

    - 区分が『図面のみ』『ULKESのみ』ならそのまま対応する区分を返す（青／緑）。
    - 区分が『両方』の場合、図面個数とULKES個数が一致すれば無色（MATCH）、
      不一致（片方が欠損の場合も含む）なら黄（MISMATCH）。
    """
    if kubun == KUBUN_DXF_ONLY:
        return ROW_STYLE_DXF_ONLY
    if kubun == KUBUN_ULKES_ONLY:
        return ROW_STYLE_ULKES_ONLY
    if pd.notna(a_count) and pd.notna(b_count) and a_count == b_count:
        return ROW_STYLE_MATCH
    return ROW_STYLE_MISMATCH


def compare_symbols(dxf_counter: Counter, ulkes_symbol_counter: Counter) -> pd.DataFrame:
    """符号単位で図面側とULKES側を比較する。

    columns: DIFF_COLUMNS。区分 ∈ {KUBUN_BOTH, KUBUN_DXF_ONLY, KUBUN_ULKES_ONLY}。
    無い側の個数は pd.NA。符号昇順（sorted）。
    """
    labels = sorted(set(dxf_counter) | set(ulkes_symbol_counter))
    rows = []
    for lbl in labels:
        in_dxf, in_ulkes = lbl in dxf_counter, lbl in ulkes_symbol_counter
        kubun = (
            KUBUN_BOTH if (in_dxf and in_ulkes)
            else (KUBUN_DXF_ONLY if in_dxf else KUBUN_ULKES_ONLY)
        )
        rows.append({
            '符号': lbl,
            '区分': kubun,
            '図面個数': dxf_counter.get(lbl, pd.NA),
            'ULKES個数': ulkes_symbol_counter.get(lbl, pd.NA),
        })
    df = pd.DataFrame(rows, columns=DIFF_COLUMNS)
    df['図面個数'] = df['図面個数'].astype('Int64')
    df['ULKES個数'] = df['ULKES個数'].astype('Int64')
    return df


def compare_prefixes(
    dxf_counter: Counter, ulkes_symbol_counter: Counter, ulkes_prefix_counter: Counter,
) -> pd.DataFrame:
    """構成数超過補完（`{prefix}?{ddd}`）が発生したプレフィックスについて、
    図面側合計とULKES側合計を比較する。

    - 図面合計: `dxf_counter` のうち、そのプレフィックスで始まる符号
      （`extract_alphabetic_part()` が一致するもの）の個数の合計。
    - ULKES合計: `ulkes_symbol_counter` のうち同条件の合計に加え、
      `ulkes_prefix_counter[prefix]`（帰属先不明の補完分）を足したもの。

    補完が発生していないプレフィックスは対象外（0行なら比較の必要がない）。
    columns: PREFIX_COLUMNS。プレフィックス昇順（sorted）。
    """
    prefixes = sorted(ulkes_prefix_counter.keys())
    rows = []
    for prefix in prefixes:
        dxf_total = sum(
            cnt for lbl, cnt in dxf_counter.items()
            if extract_alphabetic_part(lbl) == prefix
        )
        ulkes_total = sum(
            cnt for lbl, cnt in ulkes_symbol_counter.items()
            if extract_alphabetic_part(lbl) == prefix
        )
        ulkes_total += ulkes_prefix_counter[prefix]
        rows.append({'プレフィックス': prefix, '図面合計': dxf_total, 'ULKES合計': ulkes_total})
    return pd.DataFrame(rows, columns=PREFIX_COLUMNS)


def compare_pair(dxf_counter: Counter, ulkes_symbols) -> dict:
    """1つの図番ペアについて、符号単位・プレフィックス単位の比較結果を返す。

    dxf_counter: DXF-extract-labels側の {ラベル: 個数}（NFKC正規化済み想定）。
    ulkes_symbols: `extract_circuit_symbols()` が返した展開済み記号のリスト
                   （未正規化。`_`分解・過不足補完は既に適用済み）。

    戻り値: {'symbol_df': DataFrame(DIFF_COLUMNS), 'prefix_df': DataFrame(PREFIX_COLUMNS)}
    """
    ulkes_symbol_counter, ulkes_prefix_counter = classify_symbols(ulkes_symbols)
    return {
        'symbol_df': compare_symbols(dxf_counter, ulkes_symbol_counter),
        'prefix_df': compare_prefixes(dxf_counter, ulkes_symbol_counter, ulkes_prefix_counter),
    }


def pair_by_drawing_number(dxf_map: dict, ulkes_map: dict) -> tuple:
    """図番をキーに DXF側・ULKES側をペアリングする。

    dxf_map: {図番: Counter}（`dxf_labels_reader.read_dxf_labels()` の
              `by_drawing_number`）。
    ulkes_map: {図番: list[str]}（アセンブリ番号ごとの展開済み記号リスト。
              `build_ulkes_symbol_map()` の戻り値）。

    戻り値: (pairs, dxf_only, ulkes_only) いずれも図番の昇順リスト。
    """
    dxf_keys = set(dxf_map)
    ulkes_keys = set(ulkes_map)
    pairs = sorted(dxf_keys & ulkes_keys)
    dxf_only = sorted(dxf_keys - ulkes_keys)
    ulkes_only = sorted(ulkes_keys - dxf_keys)
    return pairs, dxf_only, ulkes_only


def build_ulkes_symbol_map(entries) -> tuple:
    """(アセンブリ番号, symbols, ファイル名) のリストから {図番: symbols} を構築する。

    同一アセンブリ番号が複数ファイルで指定された場合、最初の1件を採用し、
    以降は警告メッセージ（無視したファイル名）を積む。

    戻り値: (dict, list[str])
    """
    result = {}
    warnings = []
    for assembly_number, symbols, filename in entries:
        if assembly_number in result:
            warnings.append(
                f"アセンブリ番号 '{assembly_number}' が複数ファイルで指定されています"
                f"（'{filename}' は無視され、最初に処理したファイルの内容を使用します）"
            )
            continue
        result[assembly_number] = symbols
    return result, warnings
