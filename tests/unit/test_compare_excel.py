"""model.compare_excel の単体テスト（合成データ）。"""
import pandas as pd

from model.compare_excel import create_comparison_excel_output


def _minimal_result(**overrides):
    result = {
        'pairs': [],
        'dxf_only': [],
        'ulkes_only': [],
        'no_expansion': [],
        'per_pair': {},
        'warnings': [],
    }
    result.update(overrides)
    return result


def test_create_comparison_excel_output_with_no_expansion_section():
    """no_expansionキー（ファイルごとにグループ化）があればサマリーシートに
    反映され、例外なくExcelが生成される。"""
    symbol_df = pd.DataFrame(
        [{'符号': 'R1', '区分': '両方', '図面個数': 1, 'ULKES個数': 1}],
        columns=['符号', '区分', '図面個数', 'ULKES個数'],
    )

    result = _minimal_result(
        pairs=['EE0001-000-01A'],
        no_expansion=[
            ('EE0001-000-02A.xlsx', ['EE0001-000-03A', 'EE0001-000-04A']),
            ('EE0001-000-05A.xlsx', ['EE0001-000-03A']),
        ],
        per_pair={'EE0001-000-01A': {'symbol_df': symbol_df}},
    )

    output = create_comparison_excel_output(result)

    assert isinstance(output, bytes)
    assert len(output) > 0


def test_create_comparison_excel_output_without_no_expansion_key_is_backward_compatible():
    """no_expansionキーを省略しても例外なく動作する（既存呼び出し元との互換性）。"""
    result = _minimal_result()
    del result['no_expansion']

    output = create_comparison_excel_output(result)

    assert isinstance(output, bytes)
    assert len(output) > 0
