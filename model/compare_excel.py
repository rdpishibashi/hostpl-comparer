"""図面(DXF)側とULKES側の機器符号比較結果のExcel出力。"""
import io

import pandas as pd

from model.compare_symbols import DIFF_COLUMNS, PREFIX_COLUMNS, ROW_STYLE_COLORS, row_style


def _unique_sheet_name(name: str, used: set) -> str:
    """Excelのシート名31文字制限内で、`used` と重複しない名前を返す。"""
    base = name[:31]
    if base not in used:
        used.add(base)
        return base
    n = 2
    while True:
        suffix = f'_{n}'
        candidate = base[:31 - len(suffix)] + suffix
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def _write_count_cell(ws, row, col, value, fmt):
    if pd.notna(value):
        ws.write_number(row, col, int(value), fmt)
    else:
        ws.write_blank(row, col, None, fmt)


def _write_pair_sheet(writer, sheet_name, symbol_df, prefix_df, header_fmt, style_formats):
    workbook = writer.book
    workbook.add_worksheet(sheet_name)
    ws = writer.sheets[sheet_name]

    for col_idx, col_name in enumerate(DIFF_COLUMNS):
        ws.write(0, col_idx, col_name, header_fmt)

    row_idx = 1
    for label, kubun, a_val, b_val in symbol_df.itertuples(index=False):
        fmt = style_formats[row_style(kubun, a_val, b_val)]
        ws.write(row_idx, 0, label, fmt)
        ws.write(row_idx, 1, kubun, fmt)
        _write_count_cell(ws, row_idx, 2, a_val, fmt)
        _write_count_cell(ws, row_idx, 3, b_val, fmt)
        row_idx += 1

    if len(prefix_df) > 0:
        row_idx += 1
        ws.write(row_idx, 0, 'プレフィックス別集計（構成数超過補完分を含む）', header_fmt)
        row_idx += 1
        for col_idx, col_name in enumerate(PREFIX_COLUMNS):
            ws.write(row_idx, col_idx, col_name, header_fmt)
        row_idx += 1
        for prefix, dxf_total, ulkes_total in prefix_df.itertuples(index=False):
            style_key = 'MATCH' if dxf_total == ulkes_total else 'MISMATCH'
            fmt = style_formats[style_key]
            ws.write(row_idx, 0, prefix, fmt)
            ws.write_number(row_idx, 1, int(dxf_total), fmt)
            ws.write_number(row_idx, 2, int(ulkes_total), fmt)
            row_idx += 1

    ws.set_column(0, 0, 25)
    ws.set_column(1, 3, 14)


def _write_summary_sheet(writer, result, header_fmt, link_fmt):
    rows = [
        {'項目': '比較した図番ペア数', '値': len(result['pairs'])},
        {'項目': '図面のみの図番数', '値': len(result['dxf_only'])},
        {'項目': 'ULKESのみの図番数', '値': len(result['ulkes_only'])},
    ]
    if result.get('no_expansion'):
        rows.append({'項目': 'ULKESに図番はあるが部品展開なしの件数', '値': len(result['no_expansion'])})
    if result.get('warnings'):
        rows.append({'項目': '警告件数', '値': len(result['warnings'])})

    df = pd.DataFrame(rows, columns=['項目', '値'])
    df.to_excel(writer, sheet_name='サマリー', index=False)
    ws = writer.sheets['サマリー']
    for col_idx, col_name in enumerate(df.columns):
        ws.write(0, col_idx, col_name, header_fmt)

    next_row = len(df) + 2

    def _write_section(title, items, as_link=False):
        nonlocal next_row
        if not items:
            return
        ws.write(next_row, 0, title, header_fmt)
        next_row += 1
        for item in items:
            if as_link:
                sheet_name = item[:31]
                ws.write_url(next_row, 0, f"internal:'{sheet_name}'!A1", link_fmt, item)
            else:
                ws.write(next_row, 0, item)
            next_row += 1
        next_row += 1

    _write_section('比較した図番一覧（クリックでシートへ移動）', result['pairs'], as_link=True)
    _write_section('図面のみに存在する図番', result['dxf_only'])
    _write_section('ULKESのみに存在する図番', result['ulkes_only'])
    _write_section('ULKESに図番はあるが部品展開なし', result.get('no_expansion', []))
    _write_section('警告', result.get('warnings', []))

    ws.set_column(0, 0, 55)
    ws.set_column(1, 1, 20)


def create_comparison_excel_output(result: dict) -> bytes:
    """比較結果Excelをbytesで返す。

    result = {
        'pairs': [図番, ...]（比較したペアの図番、昇順）,
        'dxf_only': [図番, ...],
        'ulkes_only': [図番, ...],
        'no_expansion': [図番, ...]（省略可。ULKES側に図番はあるが部品展開行が
            0件だったもの。比較対象からは除外し、サマリーシートに別掲する）,
        'per_pair': {図番: {'symbol_df': DataFrame, 'prefix_df': DataFrame}},
        'warnings': [str, ...],
    }

    シート構成: サマリー → 図番ごとのシート（`pairs` の順）。
    符号単位比較の行は表示スタイル区分（青=図面のみ／緑=ULKESのみ／
    黄=両方だが個数不一致／無色=両方かつ個数一致）で色分けする
    （`model.compare_symbols.row_style()`）。
    """
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        workbook = writer.book
        header_fmt = workbook.add_format({
            'bold': True, 'bg_color': '#4472C4', 'font_color': 'white', 'border': 1,
        })
        link_fmt = workbook.add_format({'font_color': 'blue', 'underline': True})
        style_formats = {
            style_key: workbook.add_format({**(color or {}), 'border': 1})
            for style_key, color in ROW_STYLE_COLORS.items()
        }

        _write_summary_sheet(writer, result, header_fmt, link_fmt)

        used_sheet_names = {'サマリー'}
        for drawing_number in result['pairs']:
            pair_data = result['per_pair'][drawing_number]
            sheet_name = _unique_sheet_name(drawing_number, used_sheet_names)
            _write_pair_sheet(
                writer, sheet_name, pair_data['symbol_df'], pair_data['prefix_df'],
                header_fmt, style_formats,
            )

    return output.getvalue()
