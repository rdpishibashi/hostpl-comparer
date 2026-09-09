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
from model.dxf_symbol_extractor import build_dxf_symbol_map, extract_symbols_from_dxf_file
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


def dedupe_by_filename(files):
    """同名ファイルが複数選択された場合、最初の1件だけを採用する
    （同じファイルをDXF側・ULKES側でそれぞれ複数回選択してしまった場合の対策）。

    Returns:
        tuple[list, int]: (ユニークなファイルのリスト（アップロード順を維持）、
            重複として除外した件数)
    """
    seen = set()
    unique = []
    duplicate_count = 0
    for f in files:
        if f.name in seen:
            duplicate_count += 1
            continue
        seen.add(f.name)
        unique.append(f)
    return unique, duplicate_count


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


def _render_symbol_list(symbols, download_filename, key):
    """機器符号リストのプレビュー（開閉ウィンドウ内）＋テキスト保存ボタン。"""
    if not symbols:
        st.caption("機器符号はありません。")
        return

    st.dataframe(
        pd.DataFrame({"機器符号": symbols}), width="stretch", hide_index=True
    )
    st.download_button(
        "テキストファイルを保存",
        data=_symbols_to_text(symbols).encode("utf-8"),
        file_name=download_filename,
        mime="text/plain",
        key=key,
    )


def _filter_no_expansion(no_expansion_by_file, ulkes_map):
    """「部品展開なし」一覧から、実際には別のファイルで部品展開済みの図番を除く。

    ある図番が(a)あるファイルでは部品展開されて実際の機器符号リストを持ち、
    (b)別のファイルでは単なる参照行（部品展開なし）として現れる、という
    ケースが実データにある（例: EE6661-000-05Aは自身のファイルで展開されるが、
    EE6313-000-01Cのファイルには参照行としても現れる）。この場合、部品展開が
    「無い」という表示は誤解を招くため、そのファイルの一覧からは除く
    （2026-09-08、ユーザー指摘: 内容が同じで冗長）。あるファイルの一覧が
    空になった場合はそのファイル自体を結果から除く。
    """
    filtered = []
    for filename, drawing_numbers in no_expansion_by_file:
        remaining = [dn for dn in drawing_numbers if dn not in ulkes_map]
        if remaining:
            filtered.append((filename, remaining))
    return filtered


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

    dxf_map, rejected_map, no_frame_filenames, dxf_warnings = build_dxf_symbol_map(dxf_per_file)

    ulkes_entries = []  # (assembly_number, symbols, filename)
    no_expansion_by_file = []  # [(ファイル名, [図番, ...]), ...]
    duplicate_assembly_by_file = []  # [(ファイル名, [図番, ...]), ...]（ユニーク・ABC順）
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

        assemblies, no_expansion, duplicate_assembly_numbers = extract_all_assemblies(df)
        if no_expansion:
            no_expansion_by_file.append((f.name, sorted(no_expansion)))
        if duplicate_assembly_numbers:
            duplicate_assembly_by_file.append(
                (f.name, sorted(set(duplicate_assembly_numbers)))
            )
        for assembly_number, symbols in assemblies.items():
            ulkes_entries.append((assembly_number, symbols, f.name))

    ulkes_map, cross_file_warnings = build_ulkes_symbol_map(ulkes_entries)
    pl_warnings.extend(cross_file_warnings)

    no_expansion_by_file = _filter_no_expansion(no_expansion_by_file, ulkes_map)
    no_expansion_by_file = sorted(no_expansion_by_file, key=lambda item: item[0])
    duplicate_assembly_by_file = sorted(duplicate_assembly_by_file, key=lambda item: item[0])
    no_frame_filenames = sorted(no_frame_filenames)

    pairs, _dxf_only, ulkes_only = pair_by_drawing_number(dxf_map, ulkes_map)

    per_pair = {}
    for drawing_number in pairs:
        per_pair[drawing_number] = compare_pair(
            dxf_map[drawing_number], ulkes_map[drawing_number],
            rejected_map[drawing_number],
        )

    result = {
        "pairs": pairs,
        "ulkes_only": ulkes_only,
        "no_expansion": no_expansion_by_file,
        "duplicate_assembly_numbers": duplicate_assembly_by_file,
        "no_frame_filenames": no_frame_filenames,
        "per_pair": per_pair,
        "warnings": dxf_warnings + pl_warnings,
    }

    st.session_state["compare_result"] = result
    st.session_state["compare_output"] = create_comparison_excel_output(result)
    st.session_state["ulkes_map"] = ulkes_map


def _render_results():
    result = st.session_state["compare_result"]
    ulkes_map = st.session_state["ulkes_map"]

    no_expansion_total = sum(len(dns) for _f, dns in result["no_expansion"])

    st.divider()
    st.subheader("結果")
    st.info(
        f"比較した図番ペア: {len(result['pairs'])}件　/　"
        f"ULKESのみ: {len(result['ulkes_only'])}件　/　"
        f"ULKESに図番はあるが部品展開なし: {no_expansion_total}件"
    )
    for w in result["warnings"]:
        st.warning(w)

    if result["pairs"]:
        st.subheader(
            "図番ごとの比較結果",
            help=(
                "色分け:\n"
                "- 🔵 青 = 図面のみ\n"
                "- 🟢 緑 = ULKESのみ\n"
                "- 🟡 黄 = 両方にあるが個数が不一致\n"
                "- 無色 = 両方にあり個数も一致"
            ),
        )
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

                col1, col2 = st.columns(2)
                with col1:
                    st.caption("DXF側 機器符号リスト")
                    # 比較表には「dxf_display_counter」（通常候補＋ULKES側プレフィックス
                    # による救済ラベル）を使う。dxf_map[drawing_number]は救済分を含まない
                    # ため、これを使うと比較表の「両方」がプレビューに出ない食い違いが生じる。
                    dxf_symbols = _flatten_counter_sorted(pair_data["dxf_display_counter"])
                    _render_symbol_list(
                        dxf_symbols,
                        f"{drawing_number}_dxf_labels.txt",
                        key=f"dxf_dl_{drawing_number}",
                    )
                with col2:
                    st.caption("ULKES側 機器符号リスト")
                    _render_symbol_list(
                        ulkes_map[drawing_number],
                        f"{drawing_number}_partslist.txt",
                        key=f"ulkes_dl_{drawing_number}",
                    )

    if result["ulkes_only"]:
        st.markdown("**ULKESのみに存在する図番**")
        st.write("、".join(result["ulkes_only"]))

    if result["no_expansion"]:
        st.markdown("**部品リストがないULKESの図面番号**")
        for filename, drawing_numbers in result["no_expansion"]:
            st.write(f"{filename}：")
            st.write("、".join(drawing_numbers))

    if result["duplicate_assembly_numbers"]:
        st.markdown("**複数回記載されているULKESの図面番号**")
        for filename, drawing_numbers in result["duplicate_assembly_numbers"]:
            st.write(f"{filename}：")
            st.write("、".join(drawing_numbers))

    if result["no_frame_filenames"]:
        st.markdown("**図面枠を検出できないDXFファイル**")
        st.write("、".join(result["no_frame_filenames"]))

    st.divider()
    st.download_button(
        "比較結果をダウンロード",
        data=st.session_state["compare_output"],
        file_name="symbol_comparison.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        key="compare_download",
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
            "- DXFファイル（複数可）とULKES Excelパーツリスト（複数可）をアップロードして"
            "ください。\n"
            "- **DXFの図番はファイル名（拡張子を除いた部分）をそのまま使います**"
            "（図面データからの図番抽出は行いません）。ファイル名を図番と一致させて"
            "おいてください。\n"
            "- ULKES PLファイルは、ファイル内の全アセンブリ（「図面番号」列の値）を"
            "自動検出して一括処理します。部品展開のない図番行（図面参照のみ）は"
            "比較対象から除外し、別途一覧表示します。\n"
            "- 図番が一致するペアについて、機器符号（符号単位）と個数をABC順で"
            "比較します。DXF側で機器符号パターンに一致しなかったラベルでも、"
            "ULKES側と同じ英字プレフィックスを持つものは機器符号として救済します。"
            "ULKES側で構成数の過不足補完により付与された`?`は、超過マーク"
            "（末尾`?`のみ）なら本体の符号として扱い、構成数不足の補完"
            "（`{prefix}?{連番3桁}`）はDXF側で「図面のみ」となっている同プレフィックス"
            "の符号へ割り当てます（割り当てきれない分は`{prefix}?`として残ります）。\n"
            "- 色分け: 青=図面のみ、緑=ULKESのみ、黄=両方にあるが個数が不一致、"
            "無色=両方にあり個数も一致。"
        )

    dxf_files = st.file_uploader(
        "DXFファイル（複数可）", accept_multiple_files=True, key="dxf_files"
    )
    pl_files = st.file_uploader(
        "ULKES Excelパーツリスト（複数可）",
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

    dxf_targets, dxf_duplicate_count = dedupe_by_filename(dxf_targets)
    pl_targets, pl_duplicate_count = dedupe_by_filename(pl_targets)
    if dxf_duplicate_count:
        st.caption(
            f"同名のDXFファイルが{dxf_duplicate_count}件重複していたため、"
            "それぞれ最初の1件のみを採用しました。"
        )
    if pl_duplicate_count:
        st.caption(
            f"同名のULKES Excelファイルが{pl_duplicate_count}件重複していたため、"
            "それぞれ最初の1件のみを採用しました。"
        )

    if not dxf_targets and not pl_targets:
        st.info("DXFファイルまたはULKES PLファイルをアップロードしてください。")
        return

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
