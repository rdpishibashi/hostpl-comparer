import io
import os
import sys
import time

import pandas as pd
import streamlit as st

# model モジュールをインポート可能にするためのパスの追加
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

from model import designator_master
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
from model.ref_designator import unnamed_region_labels
from model.region_detector import collect_unique_region_candidate_names
from config import region_detection_config
from view import region_selection

# 比較結果（「比較」実行時に生成する）。再検出・オプション切替で古くなるため消す対象
_COMPARE_RESULT_KEYS = ("compare_result", "compare_output", "ulkes_map")
# 領域検出の状態（「領域を検出」の再実行・DXF構成の変更・オプションOFFで消す）
_REGION_KEYS = (region_selection.ANALYSES_KEY, region_selection.CONFIRMED_KEY,
                "hpl_detected_files")


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


SPINNER_APPEAR_WAIT_SEC = 0.6


def _status_placeholder_after_spinner():
    """進捗表示用の `st.empty()` を返す。`st.spinner` は開始から0.5秒経ってから表示されるため、
    その前に作ると進捗メッセージが先に出て、spinner 表示後にその下へ動いてしまう。
    spinner が出るのを待ってから作る（DXF-extract-labels と同じ）。"""
    time.sleep(SPINNER_APPEAR_WAIT_SEC)
    return st.empty()


def _resolve_master_index():
    """機器符号一覧の索引を解決する（DXF-extract-labels と同じ。鍵は
    `st.secrets['DESIGNATOR_MASTER_KEY']` → 環境変数の順）。鍵・ダイジェストファイルが無い、
    または鍵が一覧生成時と異なる場合は `None`（DEF 判定なし、CAN のみ）。"""
    try:
        secret = st.secrets.get('DESIGNATOR_MASTER_KEY')
    except Exception:
        secret = None
    if not secret:
        secret = os.environ.get('DESIGNATOR_MASTER_KEY')
    digest_path = os.path.join(current_dir, 'assets', 'designator_digests.txt')
    if not secret or not os.path.isfile(digest_path):
        return None
    return designator_master.load_index(digest_path, secret)


def _progress(placeholder, start_time, message, idx, total):
    """「ラベルを抽出中」と同じ形式の進捗メッセージ（DXF-extract-labels・DXF-label-comparison と共通）。"""
    placeholder.text(
        f"{message}（{idx}/{total}件、経過 {time.time() - start_time:.1f} 秒）")


def _region_filter_for(f, analyses):
    """この DXF に適用する領域フィルタ（`dxf_symbol_extractor` の `region_filter`）。
    領域検出結果が無ければ None（従来どおり図面枠内の全ラベル）。"""
    analysis = (analyses or {}).get(f.name)
    if analysis is None:
        return None
    return {
        "analysis": analysis,
        "name_selections": region_selection.gather_name_selections(f.name, analysis),
        "excluded_names": region_selection.global_excluded_region_names(),
        "fname": f.name,
    }


def _run_comparison(dxf_files, pl_files, region_mode=False, status_placeholder=None):
    """アップロードされたDXF・ULKES PLファイルを処理し、比較結果をsession_stateに格納する。

    `region_mode`（「指定領域で比較する」）はDXF側にだけ適用する（ULKES側は常に図番の全部品）。
    `status_placeholder` に進捗メッセージ（「ラベルを抽出中: …」等）を表示する。"""
    if status_placeholder is None:
        status_placeholder = st.empty()
    start_time = time.time()
    analyses = st.session_state.get(region_selection.ANALYSES_KEY) if region_mode else None

    master_index = _resolve_master_index()
    dxf_per_file = []
    for idx, f in enumerate(dxf_files, start=1):
        _progress(status_placeholder, start_time, f"ラベルを抽出中: {f.name}", idx, len(dxf_files))
        tmp_path = save_uploadedfile(f)
        try:
            dxf_per_file.append(
                extract_symbols_from_dxf_file(
                    tmp_path, original_filename=f.name,
                    region_filter=_region_filter_for(f, analyses) if region_mode else None,
                    master_index=master_index)
            )
        finally:
            os.unlink(tmp_path)

    dxf_map, rejected_map, no_frame_filenames, dxf_warnings = build_dxf_symbol_map(dxf_per_file)
    # 同一図番は先勝ち（build_dxf_symbol_map と同じ）。対象範囲も採用した1件のものを使う
    dxf_scopes = {}
    for r in dxf_per_file:
        if r["scope"] is not None:
            dxf_scopes.setdefault(r["drawing_number"], (r["scope"], r["scope_regions"]))

    ulkes_entries = []  # (assembly_number, symbols, filename)
    no_expansion_by_file = []  # [(ファイル名, [図番, ...]), ...]
    duplicate_assembly_by_file = []  # [(ファイル名, [図番, ...]), ...]（ユニーク・ABC順）
    pl_warnings = []
    for idx, f in enumerate(pl_files, start=1):
        _progress(status_placeholder, start_time,
                  f"ULKESパーツリストを読み込み中: {f.name}", idx, len(pl_files))
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
    for idx, drawing_number in enumerate(pairs, start=1):
        _progress(status_placeholder, start_time,
                  f"図番ごとに比較中: {drawing_number}", idx, len(pairs))
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
    if region_mode:
        result["region_mode"] = True
        result["excluded_regions"] = sorted(region_selection.global_excluded_region_names())
        result["dxf_scopes"] = {dn: dxf_scopes[dn] for dn in pairs if dn in dxf_scopes}

    status_placeholder.text("比較結果のExcelを作成中...")
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
        + ("　/　指定領域で比較: ON" if result.get("region_mode") else "")
    )
    for w in result["warnings"]:
        st.warning(w)

    if result["pairs"]:
        st.subheader(
            "図番ごとの比較結果",
            help=(
                "色分け:\n"
                "- 薄いピンク = 図面のみ\n"
                "- 薄い青 = ULKESのみ\n"
                "- 薄い黄 = 両方にあるが個数が不一致\n"
                "- 薄い緑 = 両方にあり個数も一致"
            ),
        )
        for drawing_number in result["pairs"]:
            pair_data = result["per_pair"][drawing_number]
            with st.expander(drawing_number, expanded=False):
                if result.get("region_mode") and drawing_number in result.get("dxf_scopes", {}):
                    scope, scope_regions = result["dxf_scopes"][drawing_number]
                    st.caption("DXF側の対象範囲: " + scope + (
                        f"（{'、'.join(scope_regions)}）" if scope_regions else ""))
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
        with st.expander("ULKESのみに存在する図番", expanded=False):
            st.write("、".join(result["ulkes_only"]))

    if result["no_expansion"]:
        with st.expander("部品リストがないULKESの図面番号", expanded=False):
            for filename, drawing_numbers in result["no_expansion"]:
                st.write(f"{filename}：")
                st.write("、".join(drawing_numbers))

    if result["duplicate_assembly_numbers"]:
        with st.expander("複数回記載されているULKESの図面番号", expanded=False):
            for filename, drawing_numbers in result["duplicate_assembly_numbers"]:
                st.write(f"{filename}：")
                st.write("、".join(drawing_numbers))

    if result["no_frame_filenames"]:
        with st.expander("図面枠を検出できないDXFファイル", expanded=False):
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


def _clear_session_keys(keys=(), prefixes=()):
    for k in keys:
        st.session_state.pop(k, None)
    if prefixes:
        for k in list(st.session_state.keys()):
            if isinstance(k, str) and k.startswith(tuple(prefixes)):
                del st.session_state[k]


def _clear_region_state():
    _clear_session_keys(keys=_REGION_KEYS, prefixes=region_selection.SELECTION_KEY_PREFIXES)


def _render_region_section(dxf_targets):
    """「指定領域で比較する」オプションと、ON時の領域検出・領域一覧（特定・除外）・領域の確認。
    DXF側にだけ適用される（ULKES側に領域は無い）。
    戻り値: (region_mode, 「比較」ボタンを押してよいか)"""
    st.subheader("オプション")
    region_mode = st.checkbox(
        "指定領域で比較する", key="hpl_region_mode",
        help="ONにすると「領域を検出」ボタン・「領域一覧」「領域の確認」セクションが表示されます。"
             "DXF側のラベルを、「特定」した領域の内側だけにして（「特定」領域が無い図面は図面全体）、"
             "「除外」した領域の内側は除いて比較します。ULKES側は常に図番の全部品です。"
             "（検出パラメータは config.py の region_detection_config で設定します）",
    )
    if st.session_state.get("hpl_region_mode_prev") not in (None, region_mode):
        _clear_session_keys(keys=_COMPARE_RESULT_KEYS)   # オプション切替で前回の比較結果は古くなる
    st.session_state["hpl_region_mode_prev"] = region_mode
    if not region_mode:
        _clear_region_state()
        return False, True

    current_files = sorted(f.name for f in dxf_targets)
    if st.session_state.get("hpl_detected_files", current_files) != current_files:
        _clear_region_state()   # 検出後にDXFが変わったら検出結果は無効

    analyses = st.session_state.get(region_selection.ANALYSES_KEY)
    if analyses is None:
        if st.button("領域を検出", key="hpl_detect_btn", type="primary"):
            try:
                with st.spinner("領域を検出中..."):
                    status_placeholder = _status_placeholder_after_spinner()
                    analyses = region_selection.run_region_detection(
                        [(f.name, f) for f in dxf_targets],
                        region_selection.build_region_cfg(region_detection_config),
                        status_placeholder, time.time())
                    status_placeholder.empty()
                _clear_region_state()
                _clear_session_keys(keys=_COMPARE_RESULT_KEYS)
                st.session_state[region_selection.ANALYSES_KEY] = analyses
                st.session_state["hpl_detected_files"] = current_files
                st.rerun()
            except Exception as e:
                st.error(f"エラーが発生しました: {e}")
        return True, False

    # 名称のない領域も一覧に出す（表示名は ref_designator.unnamed_region_labels()）。
    # 既定はOFF（default_on_names を渡さない）——既定ONだと全図面に「特定」領域があることになり、
    # 「特定」領域が無い図面は図面全体で比較する、というフォールバックが働かなくなるため。
    unnamed = list(dict.fromkeys(
        label for analysis in analyses.values()
        for label in unnamed_region_labels(analysis).values()))
    region_selection.render_global_region_names_section(
        collect_unique_region_candidate_names(analyses) + unnamed, False,
        action="比較", allow_exclude=True)
    if not st.session_state.get(region_selection.CONFIRMED_KEY):
        return True, False
    region_selection.render_region_confirmation_section(analyses, False, action="比較")
    return True, True


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
            "の符号へ割り当てます（割り当てきれない分は、補完元の符号が図面側にもあればその"
            "行のULKES個数に加算し、無ければ`{符号}?`として残ります）。\n"
            "- 「指定領域で比較する」をONにすると、DXF側のラベルを領域で絞り込めます"
            "（ULKES側は常に図番の全部品）。「領域を検出」→「領域一覧」で、比較したい領域を"
            "「特定」、比較から外したい領域を「除外」にチェックします。「特定」した領域の内側の"
            "ラベルだけを比較し（「特定」領域が無い図面は図面全体）、「除外」した領域の内側は"
            "除きます。\n"
            "- 色分け: 薄いピンク=図面のみ、薄い青=ULKESのみ、薄い黄=両方にあるが個数が"
            "不一致、薄い緑=両方にあり個数も一致。"
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

    region_mode, region_ready = False, True
    if dxf_targets:
        region_mode, region_ready = _render_region_section(dxf_targets)
    else:
        _clear_region_state()

    st.subheader("比較の実行")
    has_input = bool(dxf_targets) or bool(pl_targets)
    compare_done = "compare_result" in st.session_state
    run_type = "secondary" if compare_done else "primary"
    run = st.button("比較", type=run_type, disabled=not has_input or not region_ready)

    if run:
        with st.spinner("処理中..."):
            status_placeholder = _status_placeholder_after_spinner()
            _run_comparison(dxf_targets, pl_targets, region_mode, status_placeholder)
            status_placeholder.empty()
        st.rerun()

    if not compare_done:
        return

    _render_results()


if __name__ == "__main__":
    main()
