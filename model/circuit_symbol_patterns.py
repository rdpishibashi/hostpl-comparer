"""機器符号（Reference Designator）パターン定義の再エクスポート層。

このプロジェクトにおける機器符号パターン定義の唯一の参照点。実体は
`model/ref_designator.py`（DXF-extract-labelsからバイト一致で複製したprimary）に
あり、このモジュールはそこから再エクスポートするだけで独自の定義は持たない。

**定義そのものをここへ物理的に切り出さないこと** — 切り出すと ref_designator.py
の primary とのバイト一致が壊れ、`diff -q`/`md5` による再同期ができなくなる
（Tools/CLAUDE.md「共有DXF処理ライブラリパターン」節の既存規約に従う）。

将来、機器符号パターンを他プロジェクトへ共通化する場合は、このモジュールが
切り出し単位になる（複製＋diff/md5照合方式。Tools/自体がGitリポジトリではなく
各プロジェクトはStreamlit Cloudへ独立デプロイされるため、実行時に共通ファイルを
参照する方式やgit submoduleは使えない）。
"""
from .ref_designator import (
    CANDIDATE_PATTERN,
    CONFIRMED_PATTERN_CATEGORIES,
    EXCLUSION_EXACT_CATEGORIES,
    EXCLUSION_REGEX_CATEGORIES,
    PATTERN_CATEGORIES,
    PATTERNS_VERSION,
    classify_judgment_detailed,
    is_confirmed_designator,
    is_ref_designator_candidate,
    matched_confirmed_category,
    matched_pattern_name,
    normalize_label,
)

__all__ = [
    "CANDIDATE_PATTERN",
    "CONFIRMED_PATTERN_CATEGORIES",
    "EXCLUSION_EXACT_CATEGORIES",
    "EXCLUSION_REGEX_CATEGORIES",
    "PATTERN_CATEGORIES",
    "PATTERNS_VERSION",
    "classify_judgment_detailed",
    "is_confirmed_designator",
    "is_ref_designator_candidate",
    "matched_confirmed_category",
    "matched_pattern_name",
    "normalize_label",
]
