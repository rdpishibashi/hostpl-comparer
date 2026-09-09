"""実データ（複数アセンブリが展開されたULKES PLファイル）による
extract_all_assemblies() の回帰テスト。

2026-09-09、ユーザーから提供。1ファイル内に122件の部品展開済みアセンブリと
156件の部品展開なし（参照のみ）図番が混在する実データで、複数アセンブリ
自動検出・ファイル内重複警告が正しく動作することを確認する（設計時点では
未提供だった実データでの検証）。

このファイルは実クライアントの構成データ（部品番号・メーカー名等を含む）
のため`.gitignore`対象。値が変わったら意図した変更か確認してから更新すること。
"""
import os

import pandas as pd
import pytest

from model.extract_symbols import extract_all_assemblies

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "sample_data")
SAMPLE_FILE = "ME26-4702-0_ZMR1_展開レベル4.xlsx"


def test_extract_all_assemblies_with_real_multi_assembly_file():
    path = os.path.join(SAMPLE_DIR, SAMPLE_FILE)
    if not os.path.exists(path):
        pytest.skip(f"実データサンプルが見つかりません: {path}")

    df = pd.read_excel(path)
    assemblies, no_expansion, warnings = extract_all_assemblies(df)

    assert len(assemblies) == 122
    assert len(no_expansion) == 156
    assert len(warnings) == 11
    assert sum(len(symbols) for symbols in assemblies.values()) == 2779

    # 重複警告はいずれも「ファイル内に複数回出現」のメッセージ形式であること
    for w in warnings:
        assert "複数回出現しています" in w
