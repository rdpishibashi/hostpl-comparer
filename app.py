import io
import os
import sys

import pandas as pd
import streamlit as st

# model モジュールをインポート可能にするためのパスの追加
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

from model.common_utils import save_uploadedfile
from model.compare_excel import create_comparison_excel_output
from model.compare_symbols import (
    ROW_STYLE_COLORS,
    build_ulkes_symbol_map,
    compare_pair,
    pair_by_drawing_number,
    row_style,
)
from model.dxf_symbol_extractor import (
    build_dxf_symbol_map,
    drawing_number_from_filename,
    extract_symbols_from_dxf_file,
)
from model.extract_symbols import REQUIRED_COLUMNS, extract_all_assemblies


@st.cache_data(max_entries=20, ttl=3600)
def load_excel(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes))


def is_dxf_file(filename: str) -> bool:
    return os.path.basename(filename).lower().endswith(".dxf")


def is_excel_file(filename: str) -> bool:
    base = os.path.basename(filename)
    if base.startswith("~$"):  # Excelのロックファイル
        return False
    return base.lower().endswith(".xlsx")


def _symbols_to_text(symbols) -> str:
    return "".join(f"{s}\n" for s in symbols)


def _flatten_counter_sorted(counter) -> list:
    """CounterをABC順に展開し、1シンボル1行のリストにする（個数分繰り返す）。"""
    flat = []
    for label in sorted(counter):
        flat.extend([label] * counter[label])
    return flat


def _for_compare_display(symbol_df: pd.DataFrame) -> pd.DataFrame:
    """画面表示用に欠損の個数を空欄にする。"""
    disp = symbol_df.copy()
    for col in ("図面個数", "ULKES個数"):
        disp[col] = disp[col].apply(lambda v: "" if pd.isna(v) else str(int(v)))
    return disp


def _compare_row_style_factory(symbol_df: pd.DataFrame):
    """`_for_compare_display()` は個数列を表示用文字列に変換するため、色分けの
    判定（`row_style()`、個数の一致比較を含む）は変換前の元データを使う。"""

    def _row_style(row):
        raw = symbol_df.loc[row.name]
        style_key = row_style(raw["区分"], raw["図面個数"], raw["ULKES個数"])
        colors = ROW_STYLE_COLORS[style_key]
        css = (
            f"background-color: {colors['bg_color']}; color: {colors['font_color']}"
            if colors
            else ""
        )
        return [css] * len(row)

    return _row_style


def _render_symbol_list(symbols, unconfirmed_labels, download_filename, key):
    """機器符号リストのプレビュー（開閉ウィンドウ内）＋テキスト保存ボタン。"""
    if not symbols:
        st.caption("機器符号はありません。")
        return

    unconfirmed_labels = unconfirmed_labels or set()
    display_labels = [
        f"{s}（未確定）" if s in unconfirmed_labels else s for s in symbols
    ]
    st.dataframe(
        pd.DataFrame({"機器符号": display_labels}), width="stretch", hide_index=True
    )
    if unconfirmed_labels:
        st.caption(
            "（未確定）は機器符号候補ではあるが確定パターンに一致しなかったラベル"
            "（誤検出を含む可能性あり）"
        )
    st.download_button(
        "テキストファイルを保存",
        data=_symbols_to_text(symbols).encode("utf-8"),
        file_name=download_filename,
        mime="text/plain",
        key=key,
    )


def _run_comparison(dxf_files, pl_files):
    """アップロードされたDXF・ULKES PLファイルを処理し、比較結果をsession_stateに格納する。"""
    dxf_per_file = []
    for f in dxf_files:
        tmp_path = save_uploadedfile(f)
        try:
            dxf_per_file.append(
                extract_symbols_from_dxf_file(tmp_path, original_filename=f.name)
            )
        finally:
            os.unlink(tmp_path)

    dxf_map, unconfirmed_map, dxf_warnings = build_dxf_symbol_map(dxf_per_file)

    ulkes_entries = []  # (assembly_number, symbols, filename)
    no_expansion_all = []
    pl_warnings = []
    for f in pl_files:
        f.seek(0)
        try:
            df = load_excel(f.read())
        except Exception as e:
            pl_warnings.append(f"{f.name}: Excelファイルの読み込みに失敗しました: {e}")
            continue

        missing_columns = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing_columns:
            pl_warnings.append(
                f"{f.name}: 以下の列が見つかりません: " + "、".join(missing_columns)
            )
            continue

        assemblies, no_expansion, intra_file_warnings = extract_all_assemblies(df)
        for w in intra_file_warnings:
            pl_warnings.append(f"{f.name}: {w}")
        no_expansion_all.extend(no_expansion)
        for assembly_number, symbols in assemblies.items():
            ulkes_entries.append((assembly_number, symbols, f.name))

    ulkes_map, cross_file_warnings = build_ulkes_symbol_map(ulkes_entries)
    pl_warnings.extend(cross_file_warnings)

    pairs, dxf_only, ulkes_only = pair_by_drawing_number(dxf_map, ulkes_map)

    per_pair = {}
    for drawing_number in pairs:
        per_pair[drawing_number] = compare_pair(
            dxf_map[drawing_number], ulkes_map[drawing_number]
        )

    result = {
        "pairs": pairs,
        "dxf_only": dxf_only,
        "ulkes_only": ulkes_only,
        "no_expansion": sorted(set(no_expansion_all)),
        "per_pair": per_pair,
        "warnings": dxf_warnings + pl_warnings,
    }

    st.session_state["compare_result"] = result
    st.session_state["compare_output"] = create_comparison_excel_output(result)
    st.session_state["dxf_map"] = dxf_map
    st.session_state["unconfirmed_map"] = unconfirmed_map
    st.session_state["ulkes_map"] = ulkes_map


def _render_results():
    result = st.session_state["compare_result"]
    dxf_map = st.session_state["dxf_map"]
    unconfirmed_map = st.session_state["unconfirmed_map"]
    ulkes_map = st.session_state["ulkes_map"]

    st.divider()
    st.subheader("結果")
    st.info(
        f"比較した図番ペア: {len(result['pairs'])}件　/　"
        f"図面のみ: {len(result['dxf_only'])}件　/　"
        f"ULKESのみ: {len(result['ulkes_only'])}件　/　"
        f"ULKESに図番はあるが部品展開なし: {len(result['no_expansion'])}件"
    )
    for w in result["warnings"]:
        st.warning(w)

    st.download_button(
        "比較結果Excelをダウンロード",
        data=st.session_state["compare_output"],
        file_name="symbol_comparison.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        key="compare_download",
    )

    if result["no_expansion"]:
        st.caption(
            "ULKESに図番はあるが部品展開なし: " + "、".join(result["no_expansion"])
        )

    if result["pairs"]:
        st.subheader("図番ごとの比較結果")
        for drawing_number in result["pairs"]:
            pair_data = result["per_pair"][drawing_number]
            with st.expander(drawing_number, expanded=False):
                st.dataframe(
                    _for_compare_display(pair_data["symbol_df"]).style.apply(
                        _compare_row_style_factory(pair_data["symbol_df"]), axis=1
                    ),
                    width="stretch",
                    hide_index=True,
                )
                if len(pair_data["prefix_df"]) > 0:
                    st.caption("プレフィックス別集計（構成数超過補完分を含む）")
                    st.dataframe(
                        pair_data["prefix_df"], width="stretch", hide_index=True
                    )

                col1, col2 = st.columns(2)
                with col1:
                    st.caption("DXF側 機器符号リスト")
                    dxf_symbols = _flatten_counter_sorted(dxf_map[drawing_number])
                    _render_symbol_list(
                        dxf_symbols,
                        unconfirmed_map.get(drawing_number, set()),
                        f"{drawing_number}_dxf_labels.txt",
                        key=f"dxf_dl_{drawing_number}",
                    )
                with col2:
                    st.caption("ULKES側 機器符号リスト")
                    _render_symbol_list(
                        ulkes_map[drawing_number],
                        set(),
                        f"{drawing_number}_partslist.txt",
                        key=f"ulkes_dl_{drawing_number}",
                    )

    if result["dxf_only"]:
        st.subheader("図面のみに存在する図番")
        for drawing_number in result["dxf_only"]:
            with st.expander(drawing_number, expanded=False):
                dxf_symbols = _flatten_counter_sorted(dxf_map[drawing_number])
                _render_symbol_list(
                    dxf_symbols,
                    unconfirmed_map.get(drawing_number, set()),
                    f"{drawing_number}_dxf_labels.txt",
                    key=f"dxf_only_dl_{drawing_number}",
                )

    if result["ulkes_only"]:
        st.subheader("ULKESのみに存在する図番")
        for drawing_number in result["ulkes_only"]:
            with st.expander(drawing_number, expanded=False):
                _render_symbol_list(
                    ulkes_map[drawing_number],
                    set(),
                    f"{drawing_number}_partslist.txt",
                    key=f"ulkes_only_dl_{drawing_number}",
                )


def main():
    st.set_page_config(page_title="HostPL Comparer", page_icon="🔍", layout="wide")
    st.title("HostPL Comparer")
    st.write(
        "DXFファイルとULKESパーツリスト(PL)から機器符号を抽出し、"
        "図番が一致するもの同士を比較します。"
    )

    with st.expander("ℹ️ プログラム説明", expanded=False):
        st.markdown(
            "- DXFファイル（複数可）とULKESパーツリストExcel（複数可）をアップロードして"
            "ください。\n"
            "- **DXFの図番はファイル名（拡張子を除いた部分）をそのまま使います**"
            "（図面データからの図番抽出は行いません）。ファイル名を図番と一致させて"
            "おいてください。\n"
            "- ULKES PLファイルは、ファイル内の全アセンブリ（「図面番号」列の値）を"
            "自動検出して一括処理します。部品展開のない図番行（図面参照のみ）は"
            "比較対象から除外し、別途一覧表示します。\n"
            "- 図番が一致するペアについて、機器符号（符号単位）と個数をABC順で"
            "比較します。ULKES側で構成数の過不足補完により付与された`?`は、"
            "超過マーク（末尾`?`のみ）なら本体の符号として、構成数不足の補完"
            "（`{prefix}?{連番3桁}`）なら特定の符号に帰属できないためプレフィックス"
            "単位の合計比較として扱います。\n"
            "- 色分け: 青=図面のみ、緑=ULKESのみ、黄=両方にあるが個数が不一致、"
            "無色=両方にあり個数も一致。"
        )

    dxf_files = st.file_uploader(
        "DXFファイル（複数選択可）", accept_multiple_files=True, key="dxf_files"
    )
    pl_files = st.file_uploader(
        "ULKESパーツリストExcelファイル（複数選択可）",
        accept_multiple_files=True,
        key="pl_files",
    )

    dxf_targets = [f for f in (dxf_files or []) if is_dxf_file(f.name)]
    pl_targets = [f for f in (pl_files or []) if is_excel_file(f.name)]

    dxf_skipped = len(dxf_files or []) - len(dxf_targets)
    pl_skipped = len(pl_files or []) - len(pl_targets)
    if dxf_skipped:
        st.caption(f"{dxf_skipped}件の`.dxf`以外のファイルは無視しました。")
    if pl_skipped:
        st.caption(f"{pl_skipped}件の`.xlsx`以外のファイルは無視しました。")

    if not dxf_targets and not pl_targets:
        st.info("DXFファイルまたはULKES PLファイルをアップロードしてください。")
        return

    if dxf_targets:
        st.subheader("DXFファイルの図番（ファイル名から決定）")
        st.caption("この図番で照合します。ファイル名が図番と一致しているか確認してください。")
        st.dataframe(
            pd.DataFrame(
                [
                    {"ファイル名": f.name, "図番": drawing_number_from_filename(f.name)}
                    for f in dxf_targets
                ]
            ),
            width="stretch",
            hide_index=True,
        )

    st.subheader("比較の実行")
    has_input = bool(dxf_targets) or bool(pl_targets)
    compare_done = "compare_result" in st.session_state
    run_type = "secondary" if compare_done else "primary"
    run = st.button("比較", type=run_type, disabled=not has_input)

    if run:
        with st.spinner("処理中..."):
            _run_comparison(dxf_targets, pl_targets)
        st.rerun()

    if not compare_done:
        return

    _render_results()


if __name__ == "__main__":
    main()
