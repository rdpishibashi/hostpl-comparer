#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re

import pandas as pd

REQUIRED_COLUMNS = ["符号", "構成コメント", "構成数", "図面番号"]


def extract_alphabetic_part(symbol):
    """
    回路記号からアルファベット部分を抽出する

    Args:
        symbol (str): 回路記号

    Returns:
        str: アルファベット部分
    """
    # アルファベット部分（先頭の連続したアルファベット）を抽出
    match = re.match(r'^([A-Za-z]+)', symbol)
    if match:
        return match.group(1)
    return ""


def _find_assembly_blocks(df):
    """「図面番号」列を走査し、ファイル内の全アセンブリブロックを検出する。

    非空セルをブロック開始（アセンブリ番号）とみなし、以降の「図面番号」が
    空白である行をそのブロックの構成行とする。次に非空セルが現れたらそこで
    そのブロックは終了し、新しいブロックが始まる（ファイル出現順を維持）。
    ブロック開始行自体は構成行に含まない。

    Returns:
        list[tuple[str, list[int]]]: [(アセンブリ番号, [構成行インデックス...]), ...]
            構成行が1件もないブロック（次の行がすぐ非空になる場合）は
            空リストを伴う要素として含まれる。
    """
    blocks = []
    current_rows = None

    for i, row in df.iterrows():
        val = row["図面番号"]
        if pd.notna(val) and str(val).strip() != "":
            current_rows = []
            blocks.append((str(val), current_rows))
        elif current_rows is not None:
            current_rows.append(i)

    return blocks


def _process_rows(df, row_indices):
    """指定した行インデックス群から回路記号リストを構築する。

    符号/構成コメントの優先順位・"_"によるセパレータ分解・構成数との過不足補完
    （不足分は末尾に"{アルファベット部分}?{3桁連番}"を追加、超過分は先頭からqty件に
    切り詰めた上で末尾から"?"を追記）を1行ずつ適用する。
    """
    circuit_symbols = []

    for idx in row_indices:
        row = df.iloc[idx]

        # 符号または構成コメントからシンボルを取得
        if pd.notna(row["構成コメント"]) and "_" in str(row["構成コメント"]):
            # 構成コメントに"_"が含まれる場合はそちらを使用
            base_symbols = str(row["構成コメント"]).split("_")
        else:
            # そうでなければ符号を使用
            symbol_str = str(row["符号"]) if pd.notna(row["符号"]) else ""
            base_symbols = symbol_str.split("_") if "_" in symbol_str else [symbol_str]

        # 数値型の場合は整数に変換する
        qty = int(row["構成数"]) if pd.notna(row["構成数"]) else 0

        # 空文字列を除外
        base_symbols = [s for s in base_symbols if s.strip()]

        # 回路記号の個数を取得
        symbol_count = len(base_symbols)

        # 最終的なシンボルリスト
        final_symbols = base_symbols.copy()

        # 回路記号の個数と構成数を比較
        if symbol_count < qty:
            # 最後の回路記号のアルファベット部分を取得
            last_alpha = ""
            if base_symbols:
                last_alpha = extract_alphabetic_part(base_symbols[-1])

            # 不足分は"rrrrr?ddd"で補完
            # rrrrrはアルファベット部分、dddは行ごとに001からのシーケンス番号
            for i in range(qty - symbol_count):
                final_symbols.append(f"{last_alpha}?{i+1:03d}")
        elif symbol_count > qty:
            # 超過分は最後から?をつける
            final_symbols = final_symbols[:qty]
            for i in range(symbol_count - qty):
                if i < len(final_symbols):
                    final_symbols[qty-i-1] = final_symbols[qty-i-1] + "?"

        # 回路記号リストに追加
        circuit_symbols.extend(final_symbols)

    # 空文字列を除外
    circuit_symbols = [s for s in circuit_symbols if s]

    return circuit_symbols


def extract_circuit_symbols(df, assembly_number):
    """
    ULKESパーツリストのDataFrameから、指定アセンブリの回路記号リストを抽出する。

    Args:
        df (pandas.DataFrame): ULKESパーツリスト（符号・構成コメント・構成数・図面番号列を含む）
        assembly_number (str): アセンブリ番号（「図面番号」列と完全一致させる文字列）

    Returns:
        tuple[list[str], int]: (回路記号リスト（Excelの行順）, 処理対象行数)

    Raises:
        ValueError: 必須列がDataFrameに存在しない場合
    """
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            raise ValueError(f"'{col}'列がExcelファイルに見つかりません")

    row_indices = []
    for name, rows in _find_assembly_blocks(df):
        if name == assembly_number:
            row_indices = rows
            break

    circuit_symbols = _process_rows(df, row_indices)

    return circuit_symbols, len(row_indices)


def extract_all_assemblies(df):
    """
    ULKESパーツリストのDataFrame内の全アセンブリを自動検出し、それぞれの回路記号
    リストを抽出する（複数アセンブリを含むPLファイルへの対応）。

    Args:
        df (pandas.DataFrame): ULKESパーツリスト（符号・構成コメント・構成数・図面番号列を含む）

    Returns:
        tuple[dict[str, list[str]], list[str], list[str]]:
            - アセンブリ番号ごとの回路記号リスト（部品展開が1行以上あるもののみ）
            - 部品展開が0行だったアセンブリ番号のリスト（ファイル出現順。図番は存在するが
              部品構成行を持たない＝図面参照行のみのケース）
            - 警告メッセージのリスト（同一アセンブリ番号がファイル内に複数回出現した場合。
              最初に出現したブロックのみを採用する）

    Raises:
        ValueError: 必須列がDataFrameに存在しない場合
    """
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            raise ValueError(f"'{col}'列がExcelファイルに見つかりません")

    result = {}
    no_expansion = []
    warnings = []
    seen = set()

    for assembly_number, row_indices in _find_assembly_blocks(df):
        if assembly_number in seen:
            warnings.append(
                f"アセンブリ番号 '{assembly_number}' がファイル内に複数回出現しています"
                f"（最初に出現したブロックのみを使用します）"
            )
            continue
        seen.add(assembly_number)

        if not row_indices:
            no_expansion.append(assembly_number)
            continue

        result[assembly_number] = _process_rows(df, row_indices)

    return result, no_expansion, warnings
