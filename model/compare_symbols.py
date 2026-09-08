"""図面(DXF)側とULKES側の機器符号を、図番をキーに比較するコアロジック
（Streamlit非依存の純関数）。

ULKES側は `extract_symbols.extract_circuit_symbols()`/`extract_all_assemblies()`
が返す展開済み記号（`_` 分解済み、構成数との過不足補完済み）を使う。補完・超過で
付与される `?` を含む記号は、以下のルールで比較に組み込む（HostPL-extractor/
TECHNICAL.md の「不足分の記号形式」節に準拠、`?` の直後が3桁の数字かどうかで
機械的に判別）:

- 末尾 `?`（超過マーク。直後に3桁数字が続かない）: `?` を除去し、本体の
  符号として符号単位比較に使う（例: `CB018A?` → `CB018A`）。
- `{prefix}?{ddd}`（構成数不足の補完。直後に3桁数字が続く）: 特定の符号には
  帰属できないため、`_transfer_prefix_completions()` でDXF側の「図面のみ」
  症候（同プレフィックス）へABC順に割り当てる（2026-09-08、要求9で導入。
  旧仕様の独立したプレフィックス別集計表は廃止した）。割り当てきれず余った分は
  `"{prefix}?"` の1行に集約し、符号単位比較表の中で「ULKESのみ」として残す。

比較キー（`comparison_key()`）は DXF側・ULKES側の両方に適用する
（全角→半角 → 括弧より前 → 前後空白除去）。DXF側には「機器符号 (仕様)」の
ように括弧で仕様情報を付記した表記が実データに存在し（例: `CB004A (3A)`）、
括弧を残したまま比較するとULKES側の `CB004A` と一致せず「ULKESのみ」の
誤検出になるため（2026-09-08、実データ計測で確認: 誤検出10件→1件に改善）。
プレビュー・テキスト出力には比較キーを使わず、原文のまま表示する。
"""
import re
from collections import Counter

import pandas as pd

from model.extract_symbols import extract_alphabetic_part

DIFF_COLUMNS = ['符号', '区分', '図面個数', 'ULKES個数']

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


_WHITESPACE_PATTERN = re.compile(r'\s+')


def comparison_key(label: str) -> str:
    """比較用のキーを返す（全角→半角 → 括弧より前 → 空白を全て除去）。

    DXF側・ULKES側どちらの生ラベルにも適用する。プレビュー・テキスト出力には
    使わない（原文のまま表示する。括弧内の仕様情報を確認できるように残すため）。

    空白は前後だけでなく**内部も含めて全て除去**する（2026-09-08、ユーザー指定）。
    DXFの手書き回路図では見栄えのための改行が、MTEXT展開時に半角スペースへ
    変換されて残ることがある（例: "MC\\n001" → "MC 001"）。機器符号として
    正当なラベルに空白が含まれることはないため、除去して問題ない。
    """
    normalized = normalize_label(str(label))
    idx = normalized.find('(')
    core = normalized[:idx] if idx >= 0 else normalized
    return _WHITESPACE_PATTERN.sub('', core)


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
    プレフィックス単位Counterに分ける。比較キーへの変換はここで行う。"""
    symbol_counter = Counter()
    prefix_counter = Counter()
    for raw in symbols:
        kind, key = classify_ulkes_symbol(comparison_key(raw))
        if kind == 'symbol':
            symbol_counter[key] += 1
        else:
            prefix_counter[key] += 1
    return symbol_counter, prefix_counter


def ulkes_prefix_set(symbols) -> set:
    """ULKES側の展開済み記号リストから、比較キーの英字プレフィックス集合を返す
    （空文字は除く）。DXF側の機器符号候補判定の救済（`rescue_by_ulkes_prefix()`）
    に使う。構成数超過補完 `{prefix}?{ddd}` からもプレフィックスを拾う
    （`extract_alphabetic_part()` は `?` の手前で止まるため、そのまま
    プレフィックスが得られる）。"""
    prefixes = set()
    for raw in symbols:
        prefix = extract_alphabetic_part(comparison_key(raw))
        if prefix:
            prefixes.add(prefix)
    return prefixes


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


def rescue_by_ulkes_prefix(rejected_counter: Counter, ulkes_symbols) -> Counter:
    """DXF側で機器符号パターンに一致しなかったラベル（`rejected_counter`）のうち、
    比較キーの英字プレフィックスがULKES側プレフィックス集合に含まれるものだけを
    救済し、**原文のまま**集計したCounterとして返す（2026-09-08、要求10）。

    原文のまま返すのは、DXF側プレビュー（`compare_pair()`が返す
    `dxf_display_counter`）に合流させ、括弧内の仕様情報を確認できるようにする
    ため（比較そのものには`compare_pair()`内で比較キーへ変換してから使う）。

    プレフィックス集合はそのペアのULKES側記号だけから求める（他図番のプレフィックス
    で誤って救済しないため）。DXFをアップロードしていてもULKESが無い図番では
    プレフィックス集合が空になり、救済は起きない。
    """
    prefixes = ulkes_prefix_set(ulkes_symbols)
    rescued = Counter()
    for label, count in rejected_counter.items():
        prefix = extract_alphabetic_part(comparison_key(label))
        if prefix and prefix in prefixes:
            rescued[label] += count
    return rescued


def _transfer_prefix_completions(
    dxf_counter: Counter, ulkes_symbol_counter: Counter, ulkes_prefix_counter: Counter,
) -> None:
    """構成数超過補完（`{機器符号}?{ddd}`）の個数を、DXF側で「図面のみ」となっている
    同じ英字プレフィックスの符号へABC順に割り当てる（2026-09-08、要求9）。

    `ulkes_symbol_counter` を直接更新する（in-place）。`ulkes_prefix_counter` は
    2026-09-09の変更で「補完元の具体的な機器符号名」ごとに個数を持つ
    （例: `CNUPS01BA`・`CNUPS1`・`CNUPSBA` はいずれも英字プレフィックス
    `CNUPS`/`CNUPSBA`を共有しうるが、`_process_rows()`が機器符号ごとに独立して
    補完を生成するため、ここでは別々の集計として扱われる——プレフィックスだけで
    束ねてしまうと、由来の異なる複数行の不足分が1つに混ざって帰属先が
    分からなくなるため）。

    各補完元の符号（`source_symbol`）について、その英字プレフィックスが一致し
    まだULKES側に無い（＝図面のみ）DXF側符号へABC順に、DXF個数を上限として
    割り当てる。割り当てきれず余った分は`"{source_symbol}?"`として1行にまとめ、
    「ULKESのみ」として残す（末尾`?`は完全一致した同名符号の行と符号名が衝突して
    合算されるのを避けるための表示上の印であり、`classify_ulkes_symbol()`で
    再解釈されることはない）。
    """
    for source_symbol in sorted(ulkes_prefix_counter):
        budget = ulkes_prefix_counter[source_symbol]
        if budget <= 0:
            continue

        fuzzy_prefix = extract_alphabetic_part(source_symbol)
        candidates = sorted(
            sym for sym in dxf_counter
            if extract_alphabetic_part(sym) == fuzzy_prefix and ulkes_symbol_counter.get(sym, 0) == 0
        )
        for sym in candidates:
            if budget <= 0:
                break
            assign = min(dxf_counter[sym], budget)
            ulkes_symbol_counter[sym] = ulkes_symbol_counter.get(sym, 0) + assign
            budget -= assign

        if budget > 0:
            key = f'{source_symbol}?'
            ulkes_symbol_counter[key] = ulkes_symbol_counter.get(key, 0) + budget


def compare_pair(dxf_counter: Counter, ulkes_symbols, rejected_counter: Counter = None) -> dict:
    """1つの図番ペアについて、符号単位の比較結果を返す。

    Args:
        dxf_counter: DXF側の機器符号候補 {ラベル: 個数}（原文のまま、比較キーへの
            変換はこの関数の内部で行う）。
        ulkes_symbols: `extract_circuit_symbols()`/`extract_all_assemblies()` が
            返した展開済み記号のリスト（未正規化。`_`分解・過不足補完は適用済み）。
        rejected_counter: DXF側で機器符号パターンに一致しなかったラベルの
            {ラベル: 個数}（省略時は救済を行わない）。

    戻り値:
        {
          'symbol_df': DataFrame(DIFF_COLUMNS),
          'dxf_display_counter': Counter（原文のまま。通常の機器符号候補＋
              救済されたラベルを合わせたもの。DXF側プレビュー表示に使う。
              救済されたラベルは元の`dxf_counter`には含まれないため、
              呼び出し元は`dxf_map[drawing_number]`ではなく必ずこちらを
              プレビューに使うこと——さもないと比較表で「両方」となっている
              符号がプレビューにだけ表示されない食い違いが生じる
              2026-09-08、ユーザー報告で発覚）,
        }
    """
    # 原文のまま合流させる（比較キーへの変換はこのあと行う）。
    # DXF側プレビューにはこちらを使う——通常候補には無い救済ラベルの
    # 括弧内の仕様情報（例: "TB005 (30A)"）を確認できるようにするため。
    dxf_display_counter = Counter(dxf_counter)
    if rejected_counter:
        dxf_display_counter.update(rescue_by_ulkes_prefix(rejected_counter, ulkes_symbols))

    keyed_dxf_counter = Counter()
    for label, count in dxf_display_counter.items():
        keyed_dxf_counter[comparison_key(label)] += count

    ulkes_symbol_counter, ulkes_prefix_counter = classify_symbols(ulkes_symbols)
    _transfer_prefix_completions(keyed_dxf_counter, ulkes_symbol_counter, ulkes_prefix_counter)

    return {
        'symbol_df': compare_symbols(keyed_dxf_counter, ulkes_symbol_counter),
        'dxf_display_counter': dxf_display_counter,
    }


def pair_by_drawing_number(dxf_map: dict, ulkes_map: dict) -> tuple:
    """図番をキーに DXF側・ULKES側をペアリングする。

    dxf_map: {図番: Counter}（`dxf_symbol_extractor.build_dxf_symbol_map()` の戻り値）。
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
