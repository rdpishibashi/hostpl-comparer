"""図面(DXF)側とULKES側の機器符号比較結果のExcel出力。"""
import io

import pandas as pd

from model.compare_symbols import DIFF_COLUMNS, ROW_STYLE_COLORS, row_style


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


def _write_pair_sheet(writer, sheet_name, symbol_df, header_fmt, style_formats):
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

    ws.set_column(0, 0, 25)
    ws.set_column(1, 3, 14)


def _write_summary_sheet(writer, result, header_fmt, link_fmt):
    no_expansion_by_file = result.get('no_expansion', [])
    no_expansion_total = sum(len(drawing_numbers) for _filename, drawing_numbers in no_expansion_by_file)
    duplicate_by_file = result.get('duplicate_assembly_numbers', [])
    no_frame_filenames = result.get('no_frame_filenames', [])

    rows = [
        {'項目': '比較した図番ペア数', '値': len(result['pairs'])},
        {'項目': 'ULKESのみの図番数', '値': len(result['ulkes_only'])},
    ]
    if no_expansion_total:
        rows.append({'項目': 'ULKESに図番はあるが部品展開なしの件数（延べ）', '値': no_expansion_total})
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

    def _write_grouped_by_file_section(title, by_file):
        """ULKESファイルごとにグループ化した図番一覧を書く（重複排除はしない）。"""
        nonlocal next_row
        if not by_file:
            return
        ws.write(next_row, 0, title, header_fmt)
        next_row += 1
        for filename, drawing_numbers in by_file:
            ws.write(next_row, 0, f'{filename}：')
            next_row += 1
            ws.write(next_row, 0, '、'.join(drawing_numbers))
            next_row += 1
        next_row += 1

    _write_section('比較した図番一覧（クリックでシートへ移動）', result['pairs'], as_link=True)
    _write_section('ULKESのみに存在する図番', result['ulkes_only'])
    _write_grouped_by_file_section('ULKESに図番はあるが部品展開なし', no_expansion_by_file)
    _write_grouped_by_file_section('複数回記載されている図面番号', duplicate_by_file)
    _write_section('図面枠を検出できないDXFファイル', no_frame_filenames)
    _write_section('警告', result.get('warnings', []))

    ws.set_column(0, 0, 55)
    ws.set_column(1, 1, 20)


def create_comparison_excel_output(result: dict) -> bytes:
    """比較結果Excelをbytesで返す。

    result = {
        'pairs': [図番, ...]（比較したペアの図番、昇順）,
        'ulkes_only': [図番, ...],
        'no_expansion': [(ULKESファイル名, [図番, ...]), ...]（省略可。ファイルの
            処理順。ULKES側に図番はあるが部品展開行が0件だったもの。比較対象からは
            除外し、サマリーシートにファイルごとにグループ化して別掲する。
            同じ図番が複数ファイルに重複して現れてもよい（ファイル横断での
            重複排除はしない））,
        'duplicate_assembly_numbers': [(ULKESファイル名, [図番, ...]), ...]（省略可。
            ファイル内で同一アセンブリ番号が複数回出現したもの。各ファイル内で
            ユニーク化・ABC順ソート済み。最初に出現したブロックのみが比較対象になる）,
        'no_frame_filenames': [DXFファイル名, ...]（省略可。図面枠検出に失敗し
            フォールバック処理したDXFファイル。出現順）,
        'per_pair': {図番: {'symbol_df': DataFrame}},
        'warnings': [str, ...],
    }

    「図面のみに存在する図番」は表示・出力しない（2026-09-09、ユーザー指定。
    DXF側の図番はファイル名そのものであり、一覧を見ても新たな情報が無いため）。
    `pair_by_drawing_number()`が返す`dxf_only`は呼び出し元で受け取っても
    このresult辞書には含めないこと。

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
                writer, sheet_name, pair_data['symbol_df'],
                header_fmt, style_formats,
            )

    return output.getvalue()
