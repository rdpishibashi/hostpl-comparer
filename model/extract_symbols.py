#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re

import pandas as pd

REQUIRED_COLUMNS = ["符号", "構成コメント", "構成数", "図面番号"]

# 「符号」列の範囲表記（例: "SSR01-08" → SSR01, SSR02, ..., SSR08）。
# 開始・終了の桁数は開始側の表記に揃える（"01"なら2桁ゼロ埋め）。
_RANGE_PATTERN = re.compile(r'^([A-Za-z]+)(\d+)-(\d+)$')


def _expand_symbol_field(value, allow_single_fallback=True):
    """「符号」または「構成コメント」列の1つの値を機器符号のリストに展開する。

    - "_"を含む場合はそれで分割する（詳細リストとしての表記）。
    - "_"を含まず「{英字}{数字}-{数字}」形式（例: "SSR01-08"）の場合は、
      数字部分を範囲として展開する（開始 > 終了の場合は範囲とみなさない）。
    - どちらでもなく `allow_single_fallback=True` の場合、そのままの値を
      単一要素のリストとして扱う。`allow_single_fallback=False` の場合は
      空リストを返す（「構成コメント」列がただの自由記述メモで機器符号の
      リストではない場合に、その文字列自体を機器符号として誤採用しない
      ようにするため。2026-09-08、実データ`ELB001`行の
      構成コメント"2接点タイプ。UPS遮断用に使用する。"で確認）。
    - 前後の空白を除去した結果が空文字列の要素は含めない。
    """
    if not value:
        return []

    if "_" in value:
        return [s for s in value.split("_") if s.strip()]

    m = _RANGE_PATTERN.match(value)
    if m:
        prefix, start_str, end_str = m.groups()
        start, end = int(start_str), int(end_str)
        if start <= end:
            width = len(start_str)
            return [f"{prefix}{i:0{width}d}" for i in range(start, end + 1)]

    if allow_single_fallback:
        return [value] if value.strip() else []
    return []


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


def _group_duplicate_rows(df, row_indices):
    """「符号」と「構成コメント」が両方とも完全に同じ行をまとめ、構成数を合算する
    （2026-09-08、ユーザー指定）。ULKESパーツリストには、同一部品が構成数を分けて
    複数行に手入力されているケースがある（実データのCNCB001W×4行、
    構成数1+1+3+2など）。まとめずに1行ずつ過不足補完すると、同じ機器符号の
    出現数が実際のDXF側の出現数（多くは1）より過大になり、本来一致するはずの
    比較が不一致になる。まとめた上で構成数を合算し、1回だけ過不足補完すること
    でこれを解消する。

    最初に出現した行の順序を維持する（構成数以外は最初の行の値をそのまま使う）。

    Returns:
        list[tuple[str, str, int]]: [(符号, 構成コメント, 合算した構成数), ...]
    """
    groups = []  # [[符号, 構成コメント, 合算構成数], ...]（出現順）
    index_by_key = {}

    for idx in row_indices:
        row = df.iloc[idx]
        symbol_field = str(row["符号"]) if pd.notna(row["符号"]) else ""
        comment_field = str(row["構成コメント"]) if pd.notna(row["構成コメント"]) else ""
        qty = int(row["構成数"]) if pd.notna(row["構成数"]) else 0

        key = (symbol_field, comment_field)
        if key in index_by_key:
            groups[index_by_key[key]][2] += qty
        else:
            index_by_key[key] = len(groups)
            groups.append([symbol_field, comment_field, qty])

    return [tuple(g) for g in groups]


def _process_rows(df, row_indices):
    """指定した行インデックス群から回路記号リストを構築する。

    「符号」と「構成コメント」が両方とも完全に同じ行は1行として扱う
    （`_group_duplicate_rows()`。構成数は合算する）。

    「符号」と「構成コメント」は手入力のため表記が食い違うことがあり、
    どちらか機器符号の**個数が多い方**を採用する（2026-09-08、ユーザー指定。
    「構成コメント」は「符号」の詳細リストという位置づけだが、入力漏れ等で
    「符号」側の方が正確な場合もあるため）。個数が同数の場合は「構成コメント」を
    優先する（従来の「構成コメントに"_"が含まれていれば優先」という規則と
    後方互換）。個数の判定・分解方法は`_expand_symbol_field()`を参照
    （"_"分割、または「符号」列の範囲表記"SSR01-08"の展開）。

    構成数との過不足補完（不足分は末尾に"{アルファベット部分}?{3桁連番}"を追加、
    超過分は先頭からqty件に切り詰めた上で末尾から"?"を追記）はグループごとに
    1回ずつ適用する。
    """
    circuit_symbols = []

    for symbol_field, comment_field, qty in _group_duplicate_rows(df, row_indices):
        symbol_candidates = _expand_symbol_field(symbol_field)
        # 構成コメントは「_」区切りまたは範囲表記の場合のみ機器符号リストとして
        # 扱う。単なる自由記述メモ（区切りなし）は機器符号として採用しない。
        comment_candidates = _expand_symbol_field(comment_field, allow_single_fallback=False)

        # 構成コメントが機器符号リストとして解釈でき、かつ個数が符号以上なら
        # 構成コメントを採用する。それ以外は符号を採用する。
        base_symbols = (
            comment_candidates if comment_candidates and len(comment_candidates) >= len(symbol_candidates)
            else symbol_candidates
        )

        # 回路記号の個数を取得
        symbol_count = len(base_symbols)

        # 最終的なシンボルリスト
        final_symbols = base_symbols.copy()

        # 回路記号の個数と構成数を比較
        if symbol_count < qty:
            if base_symbols:
                # 「_」等で分解された機器符号それぞれが、この行の構成数分だけ
                # 独立して必要と解釈する（2026-09-09、ユーザー指定）。各機器符号は
                # 既に1個確定しているため、不足分は機器符号ごとに(qty-1)個ずつ、
                # その機器符号自身の名前で補完する
                # （例: "CNUPS01BA_CNUPSBA"・構成数10 →
                #  CNUPS01BA?001〜009、CNUPSBA?001〜009 をそれぞれ生成。
                #  単一機器符号の行（例: "CNCB001W"・構成数7）でも同じ規則が
                #  適用され、CNCB001W?001〜006 を生成する）
                for symbol in base_symbols:
                    for i in range(qty - 1):
                        final_symbols.append(f"{symbol}?{i+1:03d}")
            else:
                # 符号・構成コメントともに空で機器符号が1つも取得できない行。
                # 帰属先が無いため、空文字列プレフィックスで補完する
                for i in range(qty - symbol_count):
                    final_symbols.append(f"?{i+1:03d}")
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
