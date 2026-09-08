"""model.compare_symbols の単体テスト（合成データ）。

2026-09-08の追加要求で導入した比較キー（comparison_key）・ULKESプレフィックス
救済（rescue_by_ulkes_prefix）・構成数超過補完の振替（_transfer_prefix_completions）
を中心にカバーする。組み合わせ表の番号はセッションの引き継ぎ書に対応する。
"""
from collections import Counter

from model.compare_symbols import (
    comparison_key,
    compare_pair,
    compare_symbols,
    rescue_by_ulkes_prefix,
    ulkes_prefix_set,
)


# --- comparison_key（要求10の前提。組み合わせ表#1） ---

def test_comparison_key_strips_parenthesized_suffix():
    assert comparison_key("F004AD(3A)") == "F004AD"


def test_comparison_key_strips_space_before_parenthesis():
    assert comparison_key("CB004A (3A)") == "CB004A"


def test_comparison_key_converts_fullwidth_ascii():
    assert comparison_key("ＣＮ１") == "CN1"


def test_comparison_key_no_parenthesis_is_unchanged_but_stripped():
    assert comparison_key("  R10  ") == "R10"


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

def test_transfer_promotes_dxf_only_symbol_when_budget_matches():
    """組み合わせ表#4: 補完あり・振替先ありで「両方」に昇格し、個数が消費される。"""
    dxf_counter = Counter({"CNCB001W": 1})
    ulkes_symbols = ["CNCB?001"]  # 構成数不足の補完1件

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']
    assert len(df) == 1
    row = df.iloc[0]
    assert row['符号'] == 'CNCB001W'
    assert row['区分'] == '両方'
    assert row['図面個数'] == 1
    assert row['ULKES個数'] == 1


def test_transfer_leftover_collapses_into_prefix_placeholder_row():
    """組み合わせ表#5: 補完はあるが振替先（DXF側「図面のみ」同プレフィックス）が
    無ければ、`{prefix}?` の集約行がULKESのみとして残る。"""
    dxf_counter = Counter()  # DXF側に CNCB系の符号が無い
    ulkes_symbols = ["CNCB?001", "CNCB?002"]

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']
    assert len(df) == 1
    row = df.iloc[0]
    assert row['符号'] == 'CNCB?'
    assert row['区分'] == 'ULKESのみ'
    assert row['ULKES個数'] == 2


def test_transfer_budget_exceeds_candidate_count_leaves_remainder_as_placeholder():
    """組み合わせ表#6: budgetが振替先の図面個数を上回る場合、DXF個数分だけ消費し
    残りは`{prefix}?`に集約される。"""
    dxf_counter = Counter({"CNCB001W": 1})
    ulkes_symbols = ["CNCB?001", "CNCB?002", "CNCB?003"]  # budget=3

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']

    matched = df[df['符号'] == 'CNCB001W'].iloc[0]
    assert matched['区分'] == '両方'
    assert matched['図面個数'] == 1
    assert matched['ULKES個数'] == 1  # DXF個数(1)を上限に消費

    leftover = df[df['符号'] == 'CNCB?'].iloc[0]
    assert leftover['区分'] == 'ULKESのみ'
    assert leftover['ULKES個数'] == 2  # budget(3) - 消費(1)


def test_transfer_budget_less_than_dxf_count_leaves_yellow_mismatch():
    """組み合わせ表#7: budgetが振替先の図面個数を下回る場合、ULKES個数はbudget分
    のみとなり個数不一致（黄）になる。"""
    dxf_counter = Counter({"CNCB001W": 3})
    ulkes_symbols = ["CNCB?001"]  # budget=1

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']
    row = df[df['符号'] == 'CNCB001W'].iloc[0]
    assert row['区分'] == '両方'
    assert row['図面個数'] == 3
    assert row['ULKES個数'] == 1


def test_transfer_assigns_candidates_in_abc_order():
    """同一プレフィックスの候補が複数ある場合、ABC順に割り当てる。"""
    dxf_counter = Counter({"CNCB005": 1, "CNCB001": 1})
    ulkes_symbols = ["CNCB?001"]  # budget=1 → ABC順で先頭のCNCB001が優先されるはず

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']
    assert df[df['符号'] == 'CNCB001'].iloc[0]['区分'] == '両方'
    assert df[df['符号'] == 'CNCB005'].iloc[0]['区分'] == '図面のみ'


def test_transfer_does_not_affect_already_matched_symbols():
    """既にULKES側に存在する（＝図面のみではない）DXF符号は振替対象にならない。"""
    dxf_counter = Counter({"CNCB001W": 1})
    ulkes_symbols = ["CNCB001W", "CNCB?001"]  # CNCB001Wは既に両方一致、budget=1は他へ

    result = compare_pair(dxf_counter, ulkes_symbols)
    df = result['symbol_df']
    matched = df[df['符号'] == 'CNCB001W'].iloc[0]
    assert matched['区分'] == '両方'
    assert matched['ULKES個数'] == 1  # 元々の1個のまま（振替で加算されない）

    leftover = df[df['符号'] == 'CNCB?'].iloc[0]
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
