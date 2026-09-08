"""model.compare_symbols の単体テスト（合成データ）。

2026-09-08の追加要求で導入した比較キー（comparison_key）・ULKESプレフィックス
救済（rescue_by_ulkes_prefix）・構成数超過補完の振替（_transfer_prefix_completions）
を中心にカバーする。組み合わせ表の番号はセッションの引き継ぎ書に対応する。
"""
from collections import Counter

import pandas as pd

from model.compare_symbols import (
    comparison_key,
    compare_pair,
    compare_symbols,
    rescue_by_ulkes_prefix,
    ulkes_prefix_set,
)
from model.extract_symbols import extract_circuit_symbols


def _ulkes_symbols_for_row(symbol, comment, qty):
    """1行分のULKESパーツリストから展開済み機器符号リストを作る
    （extract_circuit_symbols()経由。補完マーカーを手書きで組み立てると
    実際の生成ロジック——機器符号ごとに独立した完全な符号名で補完する、
    2026-09-09の変更——と食い違う非現実的な入力になるため）。"""
    df = pd.DataFrame(
        [("EE0001-000-01A", None, None, None), (None, symbol, comment, qty)],
        columns=["図面番号", "符号", "構成コメント", "構成数"],
    )
    symbols, _row_count = extract_circuit_symbols(df, "EE0001-000-01A")
    return symbols


# --- comparison_key（要求10の前提。組み合わせ表#1） ---

def test_comparison_key_strips_parenthesized_suffix():
    assert comparison_key("F004AD(3A)") == "F004AD"


def test_comparison_key_strips_space_before_parenthesis():
    assert comparison_key("CB004A (3A)") == "CB004A"


def test_comparison_key_converts_fullwidth_ascii():
    assert comparison_key("ＣＮ１") == "CN1"


def test_comparison_key_no_parenthesis_is_unchanged_but_stripped():
    assert comparison_key("  R10  ") == "R10"


def test_comparison_key_removes_internal_whitespace():
    """DXFの手書き回路図では見栄えのための改行がMTEXT展開時に半角スペースへ
    変換されて残ることがある（例: "MC\\n001" → "MC 001"）。実データで確認
    （2026-09-09、ユーザー報告: MC 001とMC001は同じ機器符号として扱うべき）。"""
    assert comparison_key("MC 001") == "MC001"


def test_comparison_key_removes_multiple_internal_whitespace_and_tabs():
    assert comparison_key("MC  001\t002") == "MC001002"


# --- ulkes_prefix_set / rescue_by_ulkes_prefix（組み合わせ表#2・#3・#10） ---

def test_ulkes_prefix_set_from_plain_symbols():
    assert ulkes_prefix_set(["CB004A", "ELB001", "T01"]) == {"CB", "ELB", "T"}


def test_ulkes_prefix_set_includes_completion_marker_prefix():
    # "CNCB?001" の "?ddd" は extract_alphabetic_part では "?" の手前で止まる
    assert "CNCB" in ulkes_prefix_set(["CNCB?001"])


def test_ulkes_prefix_set_empty_when_no_ulkes_symbols():
    """PLをアップロードしていない図番では救済が起きないことの前提（組み合わせ表#10）。"""
    assert ulkes_prefix_set([]) == set()


def test_rescue_by_ulkes_prefix_rescues_matching_prefix():
    """括弧前にスペースがある非候補ラベルが、ULKES側に同プレフィックスの
    症候があれば救済される（組み合わせ表#2）。戻り値は原文のまま
    （比較キーへの変換はcompare_pair()の内部で行う。DXF側プレビューで
    括弧内の仕様情報を確認できるようにするため）。"""
    rejected = Counter({"CB004A (3A)": 1})
    rescued = rescue_by_ulkes_prefix(rejected, ["CB004A"])
    assert rescued == Counter({"CB004A (3A)": 1})


def test_rescue_by_ulkes_prefix_ignores_non_matching_prefix():
    """プレフィックスが一致しない非候補ラベル（GND等）は救済されない（組み合わせ表#3）。"""
    rejected = Counter({"GND": 5})
    rescued = rescue_by_ulkes_prefix(rejected, ["CB004A"])
    assert rescued == Counter()


def test_rescue_by_ulkes_prefix_empty_when_no_ulkes_symbols():
    rejected = Counter({"CB004A (3A)": 1})
    assert rescue_by_ulkes_prefix(rejected, []) == Counter()


# --- compare_pair: 救済がULKES側と一致し「両方」になる（組み合わせ表#2の結合確認） ---

def test_compare_pair_rescued_label_matches_ulkes_symbol():
    dxf_counter = Counter()  # 候補パターンには一致しなかった想定
    rejected_counter = Counter({"CB004A (3A)": 1})
    ulkes_symbols = ["CB004A"]

    result = compare_pair(dxf_counter, ulkes_symbols, rejected_counter)
    df = result['symbol_df']
    row = df[df['符号'] == 'CB004A'].iloc[0]
    assert row['区分'] == '両方'
    assert row['図面個数'] == 1
    assert row['ULKES個数'] == 1


def test_compare_pair_without_rejected_counter_no_rescue():
    """rejected_counterを渡さなければ救済は行わない（後方互換）。"""
    dxf_counter = Counter()
    result = compare_pair(dxf_counter, ["CB004A"])
    df = result['symbol_df']
    assert (df['符号'] == 'CB004A').any()
    row = df[df['符号'] == 'CB004A'].iloc[0]
    assert row['区分'] == 'ULKESのみ'


# --- _transfer_prefix_completions（要求9、compare_pair経由。組み合わせ表#4〜#7） ---
#
# 2026-09-09の変更で、構成数不足の補完は機器符号ごとに独立して、その機器符号
# 自身の完全な名前で生成されるようになった（例: "CNCB001W"・構成数3 →
# ["CNCB001W","CNCB001W?001","CNCB001W?002"]）。以下のテストは
# `_ulkes_symbols_for_row()` で実際の生成ロジックを経由した現実的な入力を使う。

def test_transfer_promotes_different_dxf_symbol_sharing_prefix():
    """組み合わせ表#4: 構成数不足の補完が、DXF側で「図面のみ」となっている
    別の同プレフィックス符号に割り当てられ「両方」に昇格する。補完元自身
    （CNCB001W）は基本出現1個で既にDXFと一致済みのため、完全に消費されれば
    余り行は残らない。"""
    ulkes_symbols = _ulkes_symbols_for_row("CNCB001W", None, 2)  # ["CNCB001W","CNCB001W?001"]
    dxf_counter = Counter({"CNCB001W": 1, "CNCB002X": 1})

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']

    matched = df[df['符号'] == 'CNCB001W'].iloc[0]
    assert matched['区分'] == '両方'
    assert matched['ULKES個数'] == 1

    promoted = df[df['符号'] == 'CNCB002X'].iloc[0]
    assert promoted['区分'] == '両方'
    assert promoted['図面個数'] == 1
    assert promoted['ULKES個数'] == 1
    assert not (df['符号'] == 'CNCB001W?').any()  # 完全に消費されたので余りは残らない


def test_transfer_leftover_uses_source_symbol_name_when_no_dxf_candidate():
    """組み合わせ表#5: 振替先となる別のDXF符号が無ければ、補完元の機器符号名+
    『?』の1行がULKESのみとして残る。"""
    ulkes_symbols = _ulkes_symbols_for_row("CNCB001W", None, 3)  # base1 + 補完2個
    dxf_counter = Counter({"CNCB001W": 1})  # 同プレフィックスの他の符号は無い

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']

    matched = df[df['符号'] == 'CNCB001W'].iloc[0]
    assert matched['区分'] == '両方'
    assert matched['ULKES個数'] == 1

    leftover = df[df['符号'] == 'CNCB001W?'].iloc[0]
    assert leftover['区分'] == 'ULKESのみ'
    assert leftover['ULKES個数'] == 2


def test_transfer_budget_exceeds_candidate_count_leaves_remainder():
    """組み合わせ表#6: 振替先のDXF個数を上回るbudgetは、DXF個数分だけ消費し
    残りは補完元の機器符号名+『?』に集約される。"""
    ulkes_symbols = _ulkes_symbols_for_row("CNCB001W", None, 4)  # base1 + 補完3個
    dxf_counter = Counter({"CNCB001W": 1, "CNCB002X": 1})

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']

    promoted = df[df['符号'] == 'CNCB002X'].iloc[0]
    assert promoted['区分'] == '両方'
    assert promoted['図面個数'] == 1
    assert promoted['ULKES個数'] == 1  # DXF個数(1)を上限に消費

    leftover = df[df['符号'] == 'CNCB001W?'].iloc[0]
    assert leftover['区分'] == 'ULKESのみ'
    assert leftover['ULKES個数'] == 2  # budget(3) - 消費(1)


def test_transfer_budget_less_than_dxf_count_leaves_yellow_mismatch():
    """組み合わせ表#7: budgetが振替先の図面個数を下回る場合、ULKES個数はbudget分
    のみとなり個数不一致（黄）になる。補完元（CNCB999Z）自身はDXFに存在しない
    ため、その1個は「図面のみ」ではなく振替の起点にしか使わない。"""
    ulkes_symbols = _ulkes_symbols_for_row("CNCB999Z", None, 2)  # base1 + 補完1個
    dxf_counter = Counter({"CNCB001W": 3})  # 同プレフィックスの別符号、DXF個数3

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']
    row = df[df['符号'] == 'CNCB001W'].iloc[0]
    assert row['区分'] == '両方'
    assert row['図面個数'] == 3
    assert row['ULKES個数'] == 1


def test_transfer_assigns_candidates_in_abc_order():
    """同一プレフィックスの候補が複数ある場合、ABC順に割り当てる。"""
    ulkes_symbols = _ulkes_symbols_for_row("CNCB999Z", None, 2)  # base1 + 補完1個
    dxf_counter = Counter({"CNCB005": 1, "CNCB001": 1})  # いずれも未対応(図面のみ)

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']
    assert df[df['符号'] == 'CNCB001'].iloc[0]['区分'] == '両方'
    assert df[df['符号'] == 'CNCB005'].iloc[0]['区分'] == '図面のみ'


def test_transfer_does_not_reassign_budget_to_already_matched_symbol():
    """既にDXF側と一致している機器符号（CNCB001W）は、別の機器符号
    （CNCB999Z）由来の補完budgetの受け皿にはならない（`candidates`が
    `ulkes_symbol_counter.get(sym,0)==0`で除外することの確認）。"""
    ulkes_symbols = (
        _ulkes_symbols_for_row("CNCB001W", None, 1)  # ["CNCB001W"]（完全一致、補完なし）
        + _ulkes_symbols_for_row("CNCB999Z", None, 2)  # ["CNCB999Z","CNCB999Z?001"]
    )
    dxf_counter = Counter({"CNCB001W": 1})  # CNCB999Zの補完を受け取れる他のCNCB符号は無い

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']
    matched = df[df['符号'] == 'CNCB001W'].iloc[0]
    assert matched['区分'] == '両方'
    assert matched['ULKES個数'] == 1  # 元々の1個のまま（横取りされない）

    leftover = df[df['符号'] == 'CNCB999Z?'].iloc[0]
    assert leftover['区分'] == 'ULKESのみ'
    assert leftover['ULKES個数'] == 1


def test_compare_symbols_still_sorted_alphabetically():
    """比較キー適用後もABC順ソートは維持される。"""
    dxf_counter = Counter({"R10": 1})
    ulkes_counter = Counter({"C1": 1, "R10": 1})
    df = compare_symbols(dxf_counter, ulkes_counter)
    assert df['符号'].tolist() == sorted(df['符号'].tolist())


# --- dxf_display_counter（DXF側プレビューの透明性。2026-09-08、ユーザー報告で発覚） ---

def test_compare_pair_dxf_display_counter_includes_normal_candidates():
    """通常の機器符号候補はdxf_display_counterにそのまま含まれる。"""
    dxf_counter = Counter({"R10": 2})
    result = compare_pair(dxf_counter, [])
    assert result['dxf_display_counter'] == Counter({"R10": 2})


def test_compare_pair_dxf_display_counter_includes_rescued_labels_in_original_text():
    """救済されたラベルは原文のまま（括弧を含む）dxf_display_counterに合流する。
    比較表では『両方』となるのに、プレビューには一切出ないという食い違いを防ぐための
    回帰テスト。"""
    dxf_counter = Counter()
    rejected_counter = Counter({"TB005 (30A)": 1})
    ulkes_symbols = ["TB005"]

    result = compare_pair(dxf_counter, ulkes_symbols, rejected_counter)

    assert result['dxf_display_counter'] == Counter({"TB005 (30A)": 1})
    # 比較表側はキー化されて"TB005"としてULKESと一致する
    df = result['symbol_df']
    row = df[df['符号'] == 'TB005'].iloc[0]
    assert row['区分'] == '両方'


def test_compare_pair_dxf_display_counter_excludes_non_rescued_rejected_labels():
    """プレフィックスが一致せず救済されなかった非候補ラベルはdxf_display_counterに含まれない。"""
    dxf_counter = Counter()
    rejected_counter = Counter({"GND": 1})
    ulkes_symbols = ["TB005"]

    result = compare_pair(dxf_counter, ulkes_symbols, rejected_counter)

    assert result['dxf_display_counter'] == Counter()
