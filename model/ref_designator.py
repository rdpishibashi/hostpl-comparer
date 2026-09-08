"""機器符号（候補）抽出モジュール（v1.6.0）

reference_designator_candidates.xlsx（`Patterns` / `ExclusionPatterns` /
`ConfirmedPatterns` シート）を正としてパターン・除外・確定リストを実装する。
DXF-extract-labels 固有の新機能であり、他プロジェクトとの共有コピーは無い
（`utils/extract_labels.py` とは独立に保つ）。

処理の流れ:
  1. 図面枠（lineweight=frame_lineweight かつ color=7 の LINE 4本で構成）を検出
  2. 図面枠を構成するブロック（フォーマットブロック。図面情報欄・枠外位置記号
     A-F/1-8 を含む）由来の TEXT/MTEXT を丸ごと除外
  3. 図面枠内に残ったラベルを NFKC 正規化し、括弧より前の部分でパターン判定
  4. 3パターンのいずれかに一致し、かつ除外パターンに該当しないものを
     「機器符号（候補）」とする（reference_designator_candidates.xlsx の
     RemainingUnclassified シートと同じ母集団）。除外パターンに該当したもの
     （GND・TITLE・N24 等）・3パターンいずれにも一致しない文字列（`(2/5)` 等の
     記号・注記）はどちらも候補に含めない。
  5. 機器符号（候補）のうち確定パターン（`CONFIRMED_PATTERN_CATEGORIES`、
     2026-07-10）に一致するものは「確定」として自動採用される
     （「未確定ラベル」UI には表示されない）。
  6. 確定パターンに一致しなかった残りが「未確定ラベル」UI で全件レビュー対象と
     なり、ユーザーがチェックしたものだけが最終的な機器符号として出力される
     （初期状態は全て未選択）。
"""
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

import ezdxf

from .common_utils import normalize_width
from .extract_labels import extract_text_from_entity, _block_has_text_content
from .region_detector import detect_drawing_frames, assign_region_labels

# パターン・除外・確定リストの版。リスト（PATTERN_CATEGORIES /
# EXCLUSION_*_CATEGORIES / CONFIRMED_PATTERN_CATEGORIES）を変更したら上げる。
# 判断ログ（utils/decision_log.py）の patterns_version 列に記録され、
# 「パターン更新後もまだ手動判断され続けているラベル」の特定に使う。
PATTERNS_VERSION = '1.7.2'


# ============================================================
# 1. Reference Designator パターン（Patterns シートが正）
# ============================================================

# (カテゴリ名, 正規表現, 説明) — reference_designator_candidates.xlsx の
# Patterns シートと同じ3カテゴリ。CANDIDATE_PATTERN はこれらの OR で導出する
# （tools/reference_designator_analyzer.py 等、外部ツールが個別カテゴリ名を
# 参照できるよう名前付きで公開する）。
PATTERN_CATEGORIES = [
    ('hyphen_letters_digits_any', re.compile(r'^[A-Z]+-[A-Z]+[0-9]+[A-Z0-9-]*$'),
     '英字繰返し-英字繰返し + 数字繰返し + 英数字/ハイフン任意(0可)'),
    ('letters_digits_any', re.compile(r'^[A-Z]+[0-9]+[A-Z0-9-]*$'),
     '英字繰返し + 数字繰返し + 英数字/ハイフン任意(0可)'),
    ('letters_only', re.compile(r'^[A-Z]+$'),
     '英字繰返しのみ'),
]
_PATTERN_CORE = '|'.join(rx.pattern[1:-1] for _n, rx, _d in PATTERN_CATEGORIES)
CANDIDATE_PATTERN = re.compile(r'^(?:%s)$' % _PATTERN_CORE)


def matched_pattern_name(judgment: str) -> Optional[str]:
    """判定用文字列（括弧より前）がどの候補パターンに一致したかを返す
    （一致しなければ None）。"""
    for name, rx, _desc in PATTERN_CATEGORIES:
        if rx.match(judgment):
            return name
    return None


# ============================================================
# 2. 除外パターン（ExclusionPatterns シートが正、2026-07-10 確定。
#    circuit_description の「+数字1桁許容」は 2026-07-10 追加確定。
#    wiring_digit_run（数字4桁以上連続）は 2026-07-11 追加確定）
# ============================================================

_COMMON_NOUNS = {
    'ABORT', 'ACCESSORY', 'ALARM', 'ANNEAL', 'ANODE', 'AUTO', 'AUTOSTART',
    'BRAKE', 'BUSY', 'BUZZER', 'BYPASS', 'CATHODE', 'CHAMBER', 'CHANGE',
    'CHILLER', 'CIRCUIT', 'CLOSE', 'COLD', 'CONTACT', 'CONTROL',
    'CONTROLLER', 'COVER', 'CPU', 'DATA', 'DETECT', 'DEVICENET', 'DRAIN',
    'ENABLE', 'ENCODER', 'ETHERCAT', 'ETHERNET', 'EXHAUST', 'EXTEND',
    'FAIL', 'FLOW', 'FREE', 'FUNCTION', 'HDMI', 'HOST', 'HOT', 'INPUT',
    'INTELOCK', 'INTERFACE', 'INTERLOCK', 'KEYBOARD', 'KEYBORD', 'LABEL',
    'LINE', 'LOCK', 'MASTER', 'MODE', 'MODULE', 'MONITOR', 'MOTOR',
    'MOUSE', 'MOVE', 'NC', 'NEG', 'NETWORK', 'NO', 'NOTE', 'NPN', 'OPEN',
    'OUTPUT', 'PANEL', 'PARAMETER', 'PLC', 'PNP', 'POS', 'POSITION',
    'PRESET', 'PRESSURE', 'PULS', 'RDY', 'RECALL', 'RECEPTACLE', 'RELAY',
    'RELEASE', 'REMOTE', 'RESET', 'RETRACT', 'RUN', 'SELECT', 'SENSOR',
    'SERIAL', 'SERVICE', 'SET', 'SETTING', 'SHUTTER', 'SIGN', 'SLAVE',
    'SLOT', 'SPARE', 'START', 'STATAUS', 'STATUS', 'STO', 'STOP',
    'SWITCH', 'SYSTEM', 'TERMINAL', 'THERMOCOUPLE', 'TIME', 'TRIGGER',
    'USB', 'VGA', 'VIDEO', 'WATCHDOG', 'WATER', 'WIRING',
}

_CIRCUIT_DESCRIPTION = {
    'AC', 'ACIN', 'AG', 'AGND', 'AOUT', 'CLR', 'COM', 'DC', 'DCIN', 'FG',
    'GND', 'IN', 'LG', 'LOAD', 'MR', 'MRR', 'OFF', 'ON', 'OUT', 'PE',
    'PGND', 'POW', 'POWER', 'POWIN', 'PWR', 'RX', 'SG', 'TX', 'VAC',
    'VCC', 'VDC', 'YOUT', 'ZERO',
}
# circuit_description は完全一致に加え「キーワード+数字1桁」も除外対象とする
# （例 OUT2, IN1, COM3。回路のI/O端子番号としてよく使われる形。2026-07-10
# ユーザー指摘）。2桁以上は対象外（例 OUT12 は除外しない＝候補として残る）。
_CIRCUIT_DESCRIPTION_REGEX = re.compile(
    r'^(?:%s)[0-9]?$' % '|'.join(sorted(_CIRCUIT_DESCRIPTION, key=len, reverse=True))
)

_UNIT_NAMES = {
    'CASE', 'CTC', 'EFEM', 'FOUP', 'LA', 'LB', 'LINEA', 'LINEB', 'LL',
    'SH', 'SHIELD', 'TM',
}

_CABLE_COLORS = {
    'BK', 'BL', 'BLACK', 'BLK', 'BLU', 'BLUE', 'BR', 'BRN', 'BROWN', 'GN',
    'GNYE', 'GRAY', 'GREEN', 'GREY', 'GRN', 'GY', 'OR', 'ORANGE', 'PINK',
    'PK', 'PU', 'PURPLE', 'RD', 'RED', 'SB', 'VIOLET', 'VT', 'WH',
    'WHITE', 'YE', 'YELLOW',
}

_TITLEBLOCK_TERMS = {
    'ANGLE', 'APPROVED', 'APPRV', 'CHECK', 'CHECKED', 'DATE', 'DESIG',
    'DESIGNED', 'DRAW', 'DRAWN', 'FINISH', 'ISSUED', 'MARK', 'MATERIAL',
    'NAME', 'REMARKS', 'REV', 'REVISION', 'SCALE', 'SHEET', 'SIZE',
    'TITLE', 'TOLERANCES', 'UNIT', 'WEIGHT',
}
# スペース/ピリオドを含む語句（UNLESS NOTED, MFG No. 等）は CANDIDATE_PATTERN
# （英大文字・数字・ハイフンのみ）に元々一致しないため除外リストに含める必要は
# ない（候補にすらならない）。図面情報枠の構造的除外（フォーマットブロック
# 丸ごと除外）が第一防衛線であり、本リストは第二防衛線。

# (カテゴリ名 -> (完全一致セット, 説明))。
EXCLUSION_EXACT_CATEGORIES = {
    'common_nouns': (_COMMON_NOUNS, '端子/スイッチ等の機能説明語（普通名詞）'),
    'unit_names': (_UNIT_NAMES, 'ユニット/モジュール名'),
    'cable_colors': (_CABLE_COLORS, 'ケーブル色（JIS配線色略号）'),
    'titleblock_terms': (_TITLEBLOCK_TERMS, '図面情報枠内のタイトル項目'),
}

# (カテゴリ名, 正規表現, 説明)。
EXCLUSION_REGEX_CATEGORIES = [
    ('single_letter_position', re.compile(r'^[A-Z]$'),
     '図形枠外の位置記号（単一英大文字）'),
    ('trailing_sign', re.compile(r'.*[+-]$'),
     '末尾が + / - で終わる（電源端子）'),
    ('wire_gauge', re.compile(r'^AWG[0-9]*$'),
     'AWG（ケーブル線径表記）'),
    ('rack_prefix', re.compile(r'^RACK[0-9]*(-[0-9]+)?$'),
     'RACK*（ユニット名）'),
    ('drawing_number', re.compile(r'^[A-Z]{2}[0-9]{4}-[0-9]{3}(-[0-9]{2})?[A-Z]?$'),
     '図番（例 EE1234-500-01A、DE3527-553-05B）'),
    ('jis_dwg_prefix', re.compile(r'^(JIS|DWG)[A-Z0-9]*$'),
     'JIS*/DWG*（図面情報枠）'),
    ('terminal_row_letter_digit', re.compile(r'^[AB][0-9]+$'),
     'A+1*/B+1*（機器端子の行番号）'),
    ('earth_terminal_digit', re.compile(r'^PE[0-9]+$'),
     'PE+1*（保護接地端子番号。例 PE1,PE2）'),
    ('phase_rail_letter_digit', re.compile(r'^[LNP][0-9]+[A-Z]*$'),
     'L/N/P+1*（相線 L1-L3・電源レール N24/P24 等。末尾の英大文字は0字以上許容、'
     '2026-07-10 英大文字繰り返しにも対応）'),
    ('io_signal_x_prefix', re.compile(r'^X[A-Z]+$'),
     'X+英字（PLC/内部信号名。例 XRST,XMCON,XPBON。X+数字は除外対象外）'),
    ('circuit_description', _CIRCUIT_DESCRIPTION_REGEX,
     '回路の説明（電源・接地・信号系統名）+数字1桁まで許容（例 OUT2,IN1,COM3）'),
    ('wiring_digit_run', re.compile(r'.*[0-9]{4,}'),
     '数字が4桁以上連続する配線ラベル（例 W1234, CN2345。ハイフン等で分断された'
     '数字は対象外。2026-07-11 ユーザー指定）'),
]


def normalize_label(label: str) -> str:
    """NFKC正規化+前後空白除去した表示用ラベルを返す（括弧は保持）。"""
    if not label:
        return ''
    return unicodedata.normalize('NFKC', label).strip()


def _judgment_text(normalized_label: str) -> str:
    """判定用文字列を返す（括弧以降を除く）。例: 'R10(2.2K)' -> 'R10'。"""
    idx = normalized_label.find('(')
    return normalized_label[:idx] if idx >= 0 else normalized_label


def classify_judgment_detailed(judgment: str) -> Tuple[str, Optional[str]]:
    """判定用文字列（括弧より前）を分類し、(status, category) を返す。

    status は 'candidate' / 'excluded' / 'no_match'。
    - 'no_match': 3パターン（Patterns シート）のいずれにも一致しない文字列
      （説明文・記号・注記等、例 `(2/5)`）。category は常に None。
    - 'excluded': Patterns には一致したが、除外パターン（ExclusionPatterns シート、
      例 GND・TITLE・N24 等）に該当したもの。明らかに Reference Designator では
      ないと確定しているため、候補にも未確定ラベルにも含めない。category は
      該当した `EXCLUSION_REGEX_CATEGORIES`/`EXCLUSION_EXACT_CATEGORIES` の名前。
    - 'candidate': Patterns に一致し、除外パターンにも該当しないもの。category は
      常に None。reference_designator_candidates.xlsx の RemainingUnclassified
      シートと同じ母集団（＝機器符号候補そのもの）で、「未確定ラベル」UI での
      レビュー対象になる（2026-07-10、実データで RemainingUnclassified の
      中身を再確認して確定: GND/INPUT/TITLE 等は除外カテゴリが付与されており
      RemainingUnclassified には含まれない＝'excluded' は表示対象外が正しい）。
    """
    if not judgment or not CANDIDATE_PATTERN.match(judgment):
        return 'no_match', None
    for name, (words, _desc) in EXCLUSION_EXACT_CATEGORIES.items():
        if judgment in words:
            return 'excluded', name
    for name, rx, _desc in EXCLUSION_REGEX_CATEGORIES:
        if rx.match(judgment):
            return 'excluded', name
    return 'candidate', None


def _classify_judgment(judgment: str) -> str:
    """`classify_judgment_detailed()` の status のみを返す簡易版。"""
    status, _category = classify_judgment_detailed(judgment)
    return status


def is_ref_designator_candidate(label: str) -> bool:
    """正規化済みラベル（表示用、括弧を含みうる）が機器符号（候補）かどうかを返す。

    判定は括弧より前の部分に対して行う。呼び出し側は `normalize_label()` で
    正規化した文字列を渡すこと（内部では再正規化しない）。
    """
    return _classify_judgment(_judgment_text(label)) == 'candidate'


def split_candidates(labels: List[str]) -> List[str]:
    """正規化済みラベルのリストから機器符号（候補）だけを抽出して返す。

    候補は Patterns（3パターン）に一致し、かつ除外パターンに該当しないもの
    （reference_designator_candidates.xlsx の RemainingUnclassified シートと
    同じ母集団）。除外パターン該当（GND・TITLE 等）・3パターン非一致
    （`(2/5)` 等の記号・注記）はいずれも結果に含めない。
    """
    return [label for label in labels
            if _classify_judgment(_judgment_text(label)) == 'candidate']


def summarize_labels(labels: List[str]) -> List[Tuple[str, int]]:
    """ラベルリストを (ラベル, 個数) にカウントし、ラベル昇順で返す。"""
    counter = Counter(labels)
    return [(lbl, counter[lbl]) for lbl in sorted(counter.keys())]


# 兄弟ラベル（連動採用）: 末尾が数字1〜2桁で、その前の文字列が一致するラベル同士は
# 「未確定ラベル」UI で採用/解除を連動させる（例: CN1・CN2・CN10）。
# 末尾3桁以上（CB001）・末尾が数字でない（X14A）・数字のみ（10）は対象外。
_SIBLING_RE = re.compile(r'^(.*\D)(\d{1,2})$')


def sibling_key(label: str) -> Optional[str]:
    """連動採用のグループキー（末尾数字1〜2桁を除いた前方文字列）を返す。

    判定は NFKC 正規化後の文字列で行う（全角 `ＣＮ１` と半角 `CN10` も同一
    グループになる）。連動対象外のラベルは None を返す。
    """
    m = _SIBLING_RE.match(normalize_label(label))
    return m.group(1) if m else None


def propagate_selection_all_files(
    checked_by_file: Dict[str, Dict[str, bool]], label: str, value: bool,
) -> None:
    """label の採用状態変更を全ファイルのチェック状態（正本）に伝播する。

    checked_by_file はファイル名→（ラベル→採用）のチェック状態。すべての
    ファイルにわたり、同一ラベル（NFKC正規化後に一致）と兄弟ラベル
    （sibling_key 一致）を value に揃える。存在しないラベルは追加しない。
    """
    norm = normalize_label(label)
    key = sibling_key(label)
    for file_checked in checked_by_file.values():
        for other in file_checked:
            if normalize_label(other) == norm or (
                    key is not None and sibling_key(other) == key):
                file_checked[other] = value


# ============================================================
# 3. 確定パターン（機器符号（候補）のうち、レビュー不要で自動採用してよいもの）
# ============================================================
#
# 機器符号（候補）＝ is_ref_designator_candidate の中でも、確実に Reference
# Designator と判定してよい形をユーザーと確定したパターン（2026-07-10、
# CN/CN-IF/R(...)/VR(...) は2026-07-10 追加確定）。一致したラベルは
# 「未確定ラベル」UI でのレビューを経ずに最終出力へ自動採用する。
# A,B の除外は single_letter_digits_except_ab（単一英字+数字）のみに適用する
# （letters_digits_2or3 系には適用しない。A1/B12等は既存の
# terminal_row_letter_digit 除外パターンで確定パターン判定より前に除外される
# ため実害はない）。
#
# 各カテゴリの判定基準（第2要素）:
#   'judgment' … 括弧より前の判定用文字列（`_judgment_text()`）に対して判定
#                （通常のパターン・除外判定と同じ基準）
#   'full'     … 正規化済みラベル全体（括弧を含む）に対して判定
#                （R(...)/VR(...) のように括弧の中身自体を問う場合に使う）

CONFIRMED_PATTERN_CATEGORIES = [
    # より限定的なパターンを先に判定する（複数一致した場合、より具体的な
    # カテゴリ名が集計・表示に反映されるようにするため。確定/未確定の結果
    # 自体はどの順でも変わらない＝いずれか1つでも一致すれば確定）。
    ('cn_single_digit', 'judgment', re.compile(r'^CN[0-9]$'),
     'CN + 数字1桁'),
    ('cn_if_prefix', 'judgment', re.compile(r'^CN-IF.*$'),
     '"CN-IF" + 任意の文字'),
    ('r_paren_suffix', 'full', re.compile(r'^R[0-9]+\(.*\)$'),
     'R + 数字繰り返し + "(" + 任意の文字 + ")"'),
    ('vr_paren_suffix', 'full', re.compile(r'^VR[0-9]+\(.*\)$'),
     'VR + 数字繰り返し + "(" + 任意の文字 + ")"'),
    ('letters_digits_2or3', 'judgment', re.compile(r'^[A-Z]+[0-9]{2,3}$'),
     '英大文字繰り返し + 数字2桁または3桁'),
    ('letters_digits_2or3_letter', 'judgment', re.compile(r'^[A-Z]+[0-9]{2,3}[A-Z]$'),
     '英大文字繰り返し + 数字2桁または3桁 + 英大文字1字'),
    ('hyphen_letters_digits_notail', 'judgment', re.compile(r'^[A-Z]+-[A-Z]+[0-9]+$'),
     '英大文字繰り返し + ハイフン + 英大文字繰り返し + 数字繰り返し（末尾に続きなし）'),
    ('single_letter_digits_except_ab', 'judgment', re.compile(r'^[C-Z][0-9]+$'),
     'A,B以外の英大文字1字 + 数字の繰り返し'),
]


def matched_confirmed_category(label: str) -> Optional[str]:
    """正規化済みラベル（括弧を含みうる）が確定パターンのいずれかに一致すれば
    カテゴリ名を、一致しなければ None を返す。

    大半のパターンは括弧より前の判定用文字列（judgment）に対して判定するが、
    括弧の中身自体を問うパターン（`r_paren_suffix`/`vr_paren_suffix`）は
    ラベル全体に対して判定する（`CONFIRMED_PATTERN_CATEGORIES` の判定基準参照）。
    """
    judgment = _judgment_text(label)
    for name, basis, rx, _desc in CONFIRMED_PATTERN_CATEGORIES:
        target = label if basis == 'full' else judgment
        if rx.match(target):
            return name
    return None


def is_confirmed_designator(label: str) -> bool:
    """正規化済みラベルが機器符号（候補）であり、かつ確定パターンにも一致するか。

    True の場合、「未確定ラベル」UI でのレビューを経ずに最終出力へ自動採用してよい。
    """
    judgment = _judgment_text(label)
    if _classify_judgment(judgment) != 'candidate':
        return False
    return matched_confirmed_category(label) is not None


# ============================================================
# 4. 図面枠検出・フォーマットブロック（図面情報欄）の構造的除外
# ============================================================
#
# 図面枠線は lineweight=frame_lineweight かつ color=7 の LINE 4本で構成される
# （region_detector.py の DEFAULT_REGION_CONFIG と同じ判定。lineweight 単独では
# 無関係な線分を誤って拾い図面枠検出が壊れることを実データで確認済みのため、
# color=7 の併用を維持する。2026-07-10 ユーザー確認）。
#
# 図面情報欄（右下のタイトルブロック）・図面枠外の位置記号（A-F, 1-8 等）は、
# 図面枠線を直接の子として持つ「フォーマットブロック」（実データでは JZB_*）の
# INSERT 由来であることを確認済み（サンプル18件で検証）。人名は増減するため、
# 個別の人名リストではなくこの構造的除外で対応する。

_FRAME_COLOR = 7


def _format_block_names(doc, frame_lineweight: int, frame_color: int = _FRAME_COLOR) -> set:
    """図面枠線を直接の子として持つブロック名の集合を返す（フォーマットブロック）。

    ネストされた INSERT の中までは辿らない（frame線はブロック定義の直接の子に
    置かれる想定。実データで確認済み）。
    """
    names = set()
    for blk in doc.blocks:
        for x in blk:
            if (x.dxftype() == 'LINE'
                    and getattr(x.dxf, 'lineweight', None) == frame_lineweight
                    and getattr(x.dxf, 'color', None) == frame_color):
                names.add(blk.name)
                break
    return names


def _collect_frame_and_labels(doc, frame_lineweight: int, frame_color: int = _FRAME_COLOR):
    """図面枠線と、フォーマットブロック由来を除いたラベルエンティティを収集する。

    戻り値: (frame_lines, label_entities)
      frame_lines: [(start, end), ...]（Vec3のまま。detect_drawing_frames に渡す）
      label_entities: TEXT/MTEXT エンティティのリスト（フォーマットブロック由来を除く）
    """
    msp = doc.modelspace()
    fmt_blocks = _format_block_names(doc, frame_lineweight, frame_color)

    frame_lines = []
    label_entities = []
    text_block_cache = {}

    def is_frame_line(e):
        return (getattr(e.dxf, 'lineweight', None) == frame_lineweight
                and getattr(e.dxf, 'color', None) == frame_color)

    for e in msp:
        t = e.dxftype()
        if t == 'LINE':
            if is_frame_line(e):
                frame_lines.append((e.dxf.start, e.dxf.end))
        elif t in ('TEXT', 'MTEXT'):
            label_entities.append(e)
        elif t == 'INSERT':
            name = e.dxf.name
            if name in fmt_blocks:
                # フォーマットブロック: 図面枠線のみ収集し、テキスト（図面情報欄・
                # 枠外位置記号）は丸ごと除外する
                try:
                    for v in e.virtual_entities():
                        if v.dxftype() == 'LINE' and is_frame_line(v):
                            frame_lines.append((v.dxf.start, v.dxf.end))
                except Exception:
                    pass
            else:
                if not _block_has_text_content(doc, name, text_block_cache):
                    continue
                try:
                    for v in e.virtual_entities():
                        if v.dxftype() in ('TEXT', 'MTEXT'):
                            label_entities.append(v)
                except Exception:
                    pass

    return frame_lines, label_entities


def collect_in_frame_labels(
    dxf_file: str,
    frame_lineweight: int = 100,
    frame_color: int = _FRAME_COLOR,
    snap: float = 2.0,
) -> Dict:
    """図面枠内・フォーマットブロック外のラベルを収集する。

    戻り値 dict:
      frames: [(xl,xr,y0,y1), ...]
      labels: [(text, x, y), ...]（重複除去済み、正規化前の原文）
      error: str | None
    """
    result = {'frames': [], 'labels': [], 'error': None}
    try:
        doc = ezdxf.readfile(dxf_file)
    except Exception as e:
        result['error'] = f'DXFファイルの読み込みに失敗しました: {e}'
        return result

    frame_lines, label_entities = _collect_frame_and_labels(doc, frame_lineweight, frame_color)
    frames = detect_drawing_frames(frame_lines, snap)
    result['frames'] = frames

    if not frames:
        result['error'] = (
            f'図面枠（太さ {frame_lineweight} の線で囲まれた枠）が見つかりませんでした。'
        )
        return result

    seen = set()
    labels = []
    for it in label_entities:
        _, clean_text, (x, y) = extract_text_from_entity(it)
        if not clean_text:
            continue
        in_frame = any(
            xl - 1 <= x <= xr + 1 and y0 - 1 <= y <= y1 + 1
            for (xl, xr, y0, y1) in frames
        )
        if not in_frame:
            continue
        key = (clean_text, round(x, 1), round(y, 1))
        if key in seen:
            continue
        seen.add(key)
        labels.append((clean_text, x, y))

    result['labels'] = labels
    return result


def _collect_all_labels_fallback(dxf_file: str) -> List[Tuple[str, float, float]]:
    """図面枠が検出できない場合のフォールバック: 図面枠フィルタなしで
    ファイル全体（modelspace）のラベルを収集する。"""
    try:
        doc = ezdxf.readfile(dxf_file)
    except Exception:
        return []
    msp = doc.modelspace()
    text_block_cache = {}
    out = []
    for e in msp:
        t = e.dxftype()
        if t in ('TEXT', 'MTEXT'):
            _, clean_text, (x, y) = extract_text_from_entity(e)
            if clean_text:
                out.append((clean_text, x, y))
        elif t == 'INSERT':
            if not _block_has_text_content(doc, e.dxf.name, text_block_cache):
                continue
            try:
                for v in e.virtual_entities():
                    if v.dxftype() in ('TEXT', 'MTEXT'):
                        _, clean_text, (x, y) = extract_text_from_entity(v)
                        if clean_text:
                            out.append((clean_text, x, y))
            except Exception:
                pass
    return out


# ============================================================
# 5. ラベル正規化・機器符号候補/確定/未確定ラベルへの分類（座標付き）
# ============================================================

def normalize_labels(
    labels: List[Tuple[str, float, float]],
) -> List[Tuple[str, float, float]]:
    """(text,x,y) リストの text を NFKC 正規化+前後空白除去する（座標は保持）。
    正規化後に空文字列になったものは除く。"""
    out = []
    for (t, x, y) in labels:
        nt = normalize_label(t)
        if nt:
            out.append((nt, x, y))
    return out


def classify_labels(
    labels: List[Tuple[str, float, float]],
) -> List[Tuple[str, float, float]]:
    """正規化済み (text,x,y) リストから機器符号（候補）だけを抽出して返す。

    候補は Patterns（3パターン）に一致し、かつ除外パターンに該当しないもの
    （reference_designator_candidates.xlsx の RemainingUnclassified シートと
    同じ母集団）。除外パターン該当（GND・TITLE・N24 等）・3パターン非一致
    （`(2/5)` 等の記号・注記）はいずれも結果に含めない（2026-07-10、実データで
    RemainingUnclassified の中身を再確認して確定）。この候補集合はさらに
    `split_confirmed()` で「確定（自動採用）」/「未確定（要レビュー）」に分かれる。
    """
    return [item for item in labels
            if _classify_judgment(_judgment_text(item[0])) == 'candidate']


def split_confirmed(
    labels: List[Tuple[str, float, float]],
) -> Tuple[List[Tuple[str, float, float]], List[Tuple[str, float, float]]]:
    """機器符号（候補）の (text,x,y) リストを (確定, 未確定) に分ける。

    確定は確定パターン（`CONFIRMED_PATTERN_CATEGORIES`）に一致するもの。
    「未確定ラベル」UI でのレビューを経ずに最終出力へ自動採用してよい
    （2026-07-10 ユーザー指定）。それ以外が「未確定ラベル」UI でのレビュー対象。
    """
    confirmed = []
    review = []
    for item in labels:
        if matched_confirmed_category(item[0]):
            confirmed.append(item)
        else:
            review.append(item)
    return confirmed, review


# ============================================================
# 6. 通常モード（領域なし）用トップレベル関数
# ============================================================

def extract_ref_designator_data(
    dxf_file: str,
    frame_lineweight: int = 100,
    frame_color: int = _FRAME_COLOR,
    original_filename: Optional[str] = None,
) -> Dict:
    """通常モード（領域なし）用: 図面枠内ラベルから機器符号（候補）を抽出する。

    機器符号（候補）のうち確定パターンに一致するもの（`confirmed_labels`）は
    「未確定ラベル」UI でのレビューを経ずに自動採用される。それ以外
    （`review_labels`）はユーザーがチェックしたものだけが最終的な出力になる
    （既定モードでは初期状態は全て未選択）。

    戻り値 dict:
      filename: str
      confirmed_labels: [(ラベル, x, y), ...]（正規化済み、確定パターン一致＝自動採用）
      review_labels: [(ラベル, x, y), ...]（正規化済み、未確定ラベルUIでのレビュー対象）
      total_in_frame: int
      warning: str | None （図面枠が見つからずフォールバックした場合等）
    """
    info = {
        'filename': original_filename or dxf_file,
        'confirmed_labels': [],
        'review_labels': [],
        'total_in_frame': 0,
        'warning': None,
    }

    collected = collect_in_frame_labels(dxf_file, frame_lineweight, frame_color)
    if collected['error']:
        info['warning'] = collected['error'] + '（図面枠内フィルタなしで全ラベルを対象にします）'
        raw_labels = _collect_all_labels_fallback(dxf_file)
    else:
        raw_labels = collected['labels']

    normalized = normalize_labels(raw_labels)
    info['total_in_frame'] = len(normalized)
    candidates = classify_labels(normalized)
    info['confirmed_labels'], info['review_labels'] = split_confirmed(candidates)
    return info


# ============================================================
# 7. 領域付きモード用: 領域名を付与した行データの構築
# ============================================================

def _aggregate_assigned_labels(assigned):
    """`assign_region_labels()` の出力 `[(text,x,y,names), ...]` から集計辞書を
    作る（`build_labeled_rows`〈named_regions 指定時〉と `build_region_output`
    で共用する自己完結的な集計ロジック）。

    戻り値: (cnt, region_of, in_region_count, label_count_per_region, region_label_counts)
      cnt: {ラベル: 出現数}
      region_of: {ラベル: {所属領域名, ...}}
      in_region_count: いずれかの領域に所属するラベルの出現件数
      label_count_per_region: {領域名: そのラベル出現数の合計}
      region_label_counts: {領域名: Counter({ラベル: 出現数})}
    """
    cnt = Counter()
    region_of = defaultdict(set)
    in_region_count = 0
    label_count_per_region = defaultdict(int)
    region_label_counts = defaultdict(Counter)
    for (text, _x, _y, names) in assigned:
        cnt[text] += 1
        if names:
            in_region_count += 1
        for n in names:
            region_of[text].add(n)
            label_count_per_region[n] += 1
            region_label_counts[n][text] += 1
    return cnt, region_of, in_region_count, label_count_per_region, region_label_counts


def build_labeled_rows(
    labels: List[Tuple[str, float, float]],
    named_regions: Optional[List[dict]] = None,
) -> List[dict]:
    """(text,x,y) リストから (ラベル,個数[,領域]) の行データを作る。

    named_regions が指定された場合は `assign_region_labels()` で領域名を
    割り当て、'領域' キー（カンマ区切り文字列）を各行に含める。
    """
    if named_regions is not None:
        assigned = assign_region_labels(labels, named_regions)
        cnt, region_of, _in_region_count, _label_count_per_region, _region_label_counts = \
            _aggregate_assigned_labels(assigned)
        return [
            {'ラベル': t, '個数': cnt[t], '領域': ', '.join(sorted(region_of[t]))}
            for t in sorted(cnt.keys())
        ]

    cnt = Counter(t for (t, _x, _y) in labels)
    return [{'ラベル': t, '個数': cnt[t]} for t in sorted(cnt.keys())]


def extract_ref_designator_rows(
    dxf_file: str,
    frame_lineweight: int = 100,
    frame_color: int = _FRAME_COLOR,
    original_filename: Optional[str] = None,
    named_regions: Optional[List[dict]] = None,
) -> Dict:
    """通常/領域付き両モード共用のトップレベル関数。

    図面枠内ラベルから機器符号（候補）の行データ（ラベル・個数[・領域]）を返す。
    named_regions を渡すと各行に '領域' キーが付与される（領域付きモード用）。

    戻り値 dict:
      filename, warning, total_in_frame: extract_ref_designator_data と同じ
      confirmed_rows: [{'ラベル':str,'個数':int[,'領域':str]}]（確定パターン一致＝自動採用）
      review_rows: [{'ラベル':str,'個数':int[,'領域':str]}]（未確定ラベルUIでのレビュー対象）
    """
    info = extract_ref_designator_data(dxf_file, frame_lineweight, frame_color, original_filename)
    return {
        'filename': info['filename'],
        'warning': info['warning'],
        'total_in_frame': info['total_in_frame'],
        'confirmed_rows': build_labeled_rows(info['confirmed_labels'], named_regions),
        'review_rows': build_labeled_rows(info['review_labels'], named_regions),
    }


def build_named_regions(
    analysis: dict,
    name_selections: dict,
    fname: str,
    start_no_name_idx: int = 0,
) -> Tuple[List[dict], int]:
    """領域付きモード用: 解析結果とユーザーが確定した名称選択から `assign_region_labels()`
    に渡す named リストを構築する（`region_detector.build_region_results()` の
    named 構築部分を機器符号（候補）パイプライン向けに複製したもの。
    region_detector.py は変更しないため、ここに独立して実装する）。

    戻り値: (named, next_no_name_idx)
    """
    named = []
    no_name_idx = start_no_name_idx
    for reg in analysis.get('regions', []):
        chosen_names = name_selections.get((fname, reg['id']), [])
        if not chosen_names and not reg.get('name_candidates'):
            no_name_idx += 1
            chosen_names = [f"no name {no_name_idx}"]
        for nm in chosen_names:
            if not nm:
                continue
            named.append({
                'polygon': reg['polygon'], 'name': normalize_width(nm),
                'id': reg['id'], 'frame': reg['frame'], 'area_pct': reg['area_pct'],
            })
    return named, no_name_idx


def build_region_output(
    labels: List[Tuple[str, float, float]],
    named: List[dict],
    sort_value: str = 'asc',
) -> Dict:
    """(text,x,y) リストと named（`build_named_regions()` の出力）から、
    `create_region_excel_output()` に渡せる1ファイル分の集計結果を作る。

    `region_detector.build_region_results()` の集計部分（1ファイル分）を
    機器符号（候補）パイプライン向けに複製したもの（region_detector.py は
    変更しないため、ここに独立して実装する）。

    戻り値 dict: rows, named（label_count 付与済み）, in_region_count,
      region_label_counts
    """
    assigned = assign_region_labels(labels, named)
    cnt, region_of, in_region_count, label_count_per_region, region_label_counts = \
        _aggregate_assigned_labels(assigned)

    rows = [
        {'ラベル': t, '個数': cnt[t], '領域': ', '.join(sorted(region_of[t]))}
        for t in cnt
    ]
    if sort_value == 'asc':
        rows.sort(key=lambda r: r['ラベル'])
    elif sort_value == 'desc':
        rows.sort(key=lambda r: r['ラベル'], reverse=True)

    for r in named:
        r['label_count'] = label_count_per_region.get(r['name'], 0)

    return {
        'rows': rows,
        'named': named,
        'in_region_count': in_region_count,
        'region_label_counts': {n: dict(c) for n, c in region_label_counts.items()},
    }


def _approved_final_labels(data: dict, approved: set) -> list:
    """`ref_pending[fname]`（確定分＋未確定ラベルUIレビュー対象）と、ユーザーが
    採用したラベル文字列の集合から、最終的に出力する (text,x,y) リストを返す
    （`build_region_results_from_pending`/`build_ref_final_from_pending` 共用）。"""
    return list(data['confirmed_labels']) + [
        item for item in data['review_labels'] if item[0] in approved
    ]


def build_region_results_from_pending(
    ref_pending: dict,
    region_analyses: dict,
    name_selections_by_file: dict,
    approved_by_file: dict,
    sort_value: str = 'asc',
) -> dict:
    """「選択完了」時（領域付きモード）の集計。`ref_pending`（未確定ラベルUI
    表示用データ）と `region_analyses`（領域検出結果）から、
    `create_region_excel_output()` に渡せる region_results を構築する。

    `name_selections_by_file`: `{fname: {(fname, reg_id): [name, ...]}}`
      （`app.py` の `_gather_name_selections(fname, analysis)` の結果を
      ファイルごとに集めたもの。チェックボックスの選択状態＝UI依存のため、
      この関数自身は計算せず呼び出し側から受け取るだけ）。
    `approved_by_file`: `{fname: {採用されたラベル文字列, ...}}`
    """
    region_results = {}
    for fname, data in ref_pending.items():
        approved = approved_by_file[fname]
        final_labels = _approved_final_labels(data, approved)
        analysis = region_analyses[fname]
        named, _ = build_named_regions(analysis, name_selections_by_file[fname], fname)
        out = build_region_output(final_labels, named, sort_value)
        review_texts = {t for t, _x, _y in data['review_labels']}
        region_results[fname] = {
            'rows': out['rows'],
            'named': out['named'],
            'frames': len(analysis.get('frames', [])),
            'regions_detected': len(analysis.get('regions', [])),
            'regions_named': len({r['id'] for r in named}),
            'total_in_frame': data['total_in_frame'],
            'filtered_count': len(review_texts - approved),
            'final_count': len(final_labels),
            'in_region_count': out['in_region_count'],
            'drawing_number': analysis.get('main_drawing_number') or '',
            'region_label_counts': out['region_label_counts'],
        }
    return region_results


def build_ref_final_from_pending(
    ref_pending: dict,
    approved_by_file: dict,
    sort_value: str = 'asc',
) -> dict:
    """「選択完了」時（通常モード）の集計。`ref_pending` から
    `create_ref_designator_excel_output()` に渡せる ref_final を構築する。"""
    ref_final = {}
    for fname, data in ref_pending.items():
        approved = approved_by_file[fname]
        final_labels = _approved_final_labels(data, approved)
        rows = build_labeled_rows(final_labels)
        if sort_value == 'desc':
            rows.sort(key=lambda r: r['ラベル'], reverse=True)
        review_texts = {t for t, _x, _y in data['review_labels']}
        ref_final[fname] = {
            'rows': rows,
            'total_in_frame': data['total_in_frame'],
            'unclassified_count': len(review_texts - approved),
            'warning': data.get('warning'),
            'main_drawing_number': data.get('main_drawing_number'),
            'source_drawing_number': data.get('source_drawing_number'),
            'title': data.get('title'),
            'subtitle': data.get('subtitle'),
        }
    return ref_final
