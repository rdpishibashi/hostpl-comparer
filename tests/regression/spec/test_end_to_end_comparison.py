"""DXF抽出→PL抽出→図番ペアリング→比較→Excel出力の一連のパイプラインを
実データ（sample_data/DXF_files/EE6312-000-01A.dxf +
sample_data/ULKES_files/EE6312-000-01A.xlsx）で確認する結合テスト。

各モジュール単体のテストは別ファイルにあるため、ここでは「実際にapp.pyが
たどる配線」がエンドツーエンドで例外なく動作し、期待する形の結果になることを
確認する。

期待値（区分ごとの件数）は2026-09-08、要求9(構成数超過補完の振替)・要求10
(ULKESプレフィックスによる救済)・比較キー(括弧除去)を実装した後に実データで
再計測した値。値が変わったら意図した変更か確認してから更新すること。
"""
import os

import pandas as pd
import pytest

from model.compare_excel import create_comparison_excel_output
from model.compare_symbols import build_ulkes_symbol_map, compare_pair, pair_by_drawing_number
from model.dxf_symbol_extractor import build_dxf_symbol_map, extract_symbols_from_dxf_file
from model.extract_symbols import extract_all_assemblies

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "sample_data")
DXF_SAMPLE_DIR = os.path.join(SAMPLE_DIR, "DXF_files")
ULKES_SAMPLE_DIR = os.path.join(SAMPLE_DIR, "ULKES_files")


def test_end_to_end_pipeline_with_real_ee6312_data():
    dxf_path = os.path.join(DXF_SAMPLE_DIR, "EE6312-000-01A.dxf")
    xlsx_path = os.path.join(ULKES_SAMPLE_DIR, "EE6312-000-01A.xlsx")
    if not os.path.exists(dxf_path) or not os.path.exists(xlsx_path):
        pytest.skip("実データサンプル（EE6312-000-01A）が見つかりません")

    # 1. DXF側の抽出
    dxf_result = extract_symbols_from_dxf_file(dxf_path, original_filename="EE6312-000-01A.dxf")
    dxf_map, rejected_map, no_frame_filenames, dxf_warnings = build_dxf_symbol_map([dxf_result])
    assert no_frame_filenames == []
    assert dxf_warnings == []
    assert "EE6312-000-01A" in dxf_map

    # 2. ULKES PL側の抽出（複数アセンブリ自動検出）
    df = pd.read_excel(xlsx_path)
    assemblies, no_expansion, ulkes_intra_warnings = extract_all_assemblies(df)
    assert "EE6312-000-01A" in assemblies
    # EE6312-000-01Aは部品展開ありのアセンブリなので、no_expansionには含まれない
    assert "EE6312-000-01A" not in no_expansion
    # サンプルのPLファイルには部品展開のない図番行が多数含まれる（既知の実データ特性）
    assert len(no_expansion) == 28

    entries = [(assembly, symbols, "EE6312-000-01A.xlsx") for assembly, symbols in assemblies.items()]
    ulkes_map, ulkes_cross_warnings = build_ulkes_symbol_map(entries)
    assert ulkes_cross_warnings == []

    # 3. 図番ペアリング
    pairs, _dxf_only, ulkes_only = pair_by_drawing_number(dxf_map, ulkes_map)
    assert "EE6312-000-01A" in pairs

    # 4. 符号単位比較（DXF側候補＋ULKESプレフィックス救済＋構成数超過補完の振替）
    per_pair = {}
    for drawing_number in pairs:
        per_pair[drawing_number] = compare_pair(
            dxf_map[drawing_number], ulkes_map[drawing_number], rejected_map[drawing_number]
        )

    pair_data = per_pair["EE6312-000-01A"]
    symbol_df = pair_data["symbol_df"]

    # 区分ごとの件数（比較キーによる括弧・空白除去、ULKESプレフィックス救済、
    # 構成数超過補完を機器符号ごとに独立して適用する方式への変更、符号+構成
    # コメントが同一の行の合算を反映した実測値。詳細はセッションの引き継ぎ書
    # 「実データで確認した事実」参照）
    kubun_counts = symbol_df["区分"].value_counts().to_dict()
    # 2026-10-14（後段）: DXF処理部品をDXF-extract-labels最新版へ同期し、AWG12・L1・A1・
    # MC001・TBCB001W等が候補になり図面のみが 30→43（NT・PT・N24PQR1・P24PQR1は非候補化）。
    # 2026-10-14（前段）: R1/S1/T1が機器符号候補から除外され（図面のみ 33→30）、余った補完
    # `X?`行6件が本体行へ合算された（ULKESのみ 7→1。残りはCB01Aのみ）。T01/T02は
    # ULKESに同名があるため救済され「両方」のまま。
    # 2026-10-16: 英大文字+数字2桁・A+数字1桁・CN+ハイフンを機器符号候補から外した
    # （DXF-extract-labels の FORCED_REGION_NAME_PATTERNS）ため、図面のみの A1・A2・PQR11・
    # DCPSPL01・DCPSPQR11 が消えて 39→34（「両方」「ULKESのみ」は不変。変更直前の実測値は
    # 39 で、上の 43 は AWG+数字 の OTHER 化以降は既に古くなっていた）。
    assert kubun_counts == {"図面のみ": 34, "両方": 21, "ULKESのみ": 1}

    # ABC順（符号昇順）ソートの確認
    labels = symbol_df["符号"].tolist()
    assert labels == sorted(labels)

    # 5. Excel出力まで例外なく完走する
    result = {
        "pairs": pairs,
        "ulkes_only": ulkes_only,
        "no_expansion": [("EE6312-000-01A.xlsx", sorted(no_expansion))],
        "per_pair": per_pair,
        "warnings": ulkes_intra_warnings,
    }
    output = create_comparison_excel_output(result)
    assert isinstance(output, bytes)
    assert len(output) > 0
