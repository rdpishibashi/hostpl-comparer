import os
import tempfile
import traceback
import re
import unicodedata

def is_invisible(e):
    """DXFの`invisible`属性（グループコード60、1=非表示）が立っている
    エンティティ、または**エンティティが所属するレイヤーがオフ/フリーズ
    されている**エンティティかを返す。CADソフト上で「非表示」に設定された
    図形（紙面には一切表示されない）は、たとえDXFファイル中に座標・テキスト
    として存在していても、図面枠検出・ラベル収集・領域検出のいずれの
    対象にもしてはならない（DXF-extract-labelsの2026-09-11の横展開に伴い
    追加。詳細はDXF-extract-labels/tests/regression/test_ref_designator.py
    のinvisible関連テストを参照）。

    呼び出し側は次の3箇所すべてでチェックする必要がある（`virtual_entities()`
    は親INSERTのinvisible属性を継承しないため、INSERT自身のチェックを
    省くと、INSERT自身がinvisibleでも展開後の中身は素通りしてしまう）:
      1. 直接配置エンティティ
      2. INSERT自身（invisibleなINSERTは中身ごと丸ごと除外する）
      3. `virtual_entities()`で展開した仮想エンティティ（親が可視でも
         個々の子エンティティにinvisibleが立っている場合があるため）

    レイヤー単位の非表示状態（DXF-extract-labels 2026-09-16の横展開に伴い追加）:
    エンティティ自身の`invisible`属性が立っていなくても、そのエンティティが
    置かれたレイヤー自体が「オフ」または「フリーズ」されていれば、画面上
    ・印刷時ともに一切表示されない。ULVAC標準の改版運用では、旧版の
    タイトルブロックをエンティティ単位のinvisible属性ではなく、専用レイヤー
    ごとオフ/フリーズして非表示にする例があり、この場合は上記の`invisible`
    属性チェックだけでは検出できない。`virtual_entities()`で展開した仮想
    エンティティも`.doc`経由で元のレイヤーテーブルを参照できるため、同じ
    チェックで対応できる（`.layer`属性は展開後も元のレイヤー名を保持し、
    親INSERTのレイヤー状態を継承しない`invisible`属性とは異なる）。
    レイヤーテーブルに存在しない・`.doc`が取得できない等の異常系は
    「非表示ではない」側にフォールバックする（誤って全除外にならないよう
    保守的に扱う）。詳細はDXF-extract-labels/tests/regression/
    test_ref_designator.pyのレイヤーoff/frozen関連テストを参照。
    """
    if bool(e.dxf.get('invisible', 0)):
        return True

    layer_name = e.dxf.get('layer', None)
    doc = getattr(e, 'doc', None)
    if layer_name and doc is not None:
        try:
            if layer_name in doc.layers:
                layer = doc.layers.get(layer_name)
                if layer.is_off() or layer.is_frozen():
                    return True
        except Exception:
            pass

    return False


def save_uploadedfile(uploadedfile):
    """アップロードされたファイルを一時ディレクトリに保存する"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploadedfile.name)[1]) as f:
        f.write(uploadedfile.getbuffer())
        return f.name

def normalize_width(text):
    """全角の英数字・記号・スペースを半角に折り畳む（NFKC正規化）。

    手書き回路DXFには同じ語が半角(SYSTEM)と全角(ＳＹＳＴＥＭ)で混在するため、
    出力ファイルの集計・記録は半角へ統一する（ユーザー指定の仕様、2026-07-03）。
    NFKC はかな・漢字には影響せず、半角カナは全角カナへ正規化される。
    """
    if not text:
        return text
    return unicodedata.normalize('NFKC', text)

def handle_error(e, show_traceback=True):
    """エラーを適切に処理して表示する"""
    import streamlit as st
    st.error(f"エラーが発生しました: {str(e)}")
    if show_traceback:
        st.error(traceback.format_exc())

def filter_non_circuit_symbols(labels, debug=False):
    """
    機器符号フォーマットに一致しないラベルをフィルタリングする

    新しい機器符号フォーマット:
    - AA+ (例: CNCNT, FB)
    - A+N+ (例: R10, CN3, PSW1)
    - A+N+A+ (例: X14A, RMSS2A)
    - AA+([内容]) (例: FB(), MSS(MOTOR))
    - A+N+([内容]) (例: R10(2.2K), MSSA(+))
    - A+N+A+([内容]) (例: U23B(DAC))

    Args:
        labels: フィルタリング対象のラベルリスト
        debug: デバッグ情報を出力するかどうか

    Returns:
        tuple: (フィルタリング後のラベルリスト, 除外されたラベル数)
    """

    patterns = [
        # 英文字のみ（2文字以上）
        r'^[A-Za-z]{2,}$',

        # 英文字+数字
        r'^[A-Za-z]+\d+$',

        # 英文字+数字+英文字
        r'^[A-Za-z]+\d+[A-Za-z]+$',

        # 英文字のみ+括弧（オプション）
        r'^[A-Za-z]{2,}\([^)]*\)$',

        # 英文字+数字+括弧（オプション）
        r'^[A-Za-z]+\d+\([^)]*\)$',

        # 英文字+数字+英文字+括弧（オプション）
        r'^[A-Za-z]+\d+[A-Za-z]+\([^)]*\)$',
    ]

    filtered_labels = []
    excluded_count = 0

    for label in labels:
        # 全角表記の機器符号（例: ＣＮ１）も半角相当で判定する。
        # 返すラベル自体は加工しない（呼び出し元は元のテキストと突き合わせる）。
        target = normalize_width(label)
        is_match = False
        for pattern in patterns:
            if re.match(pattern, target):
                is_match = True
                break

        if is_match:
            filtered_labels.append(label)
            if debug:
                print(f"✓ 機器符号として認識: {label}")
        else:
            excluded_count += 1
            if debug:
                print(f"✗ 機器符号として除外: {label}")

    return filtered_labels, excluded_count

def process_circuit_symbol_labels(labels, filter_non_parts=False, validate_ref_designators=False, debug=False):
    """
    ラベルに対して機器符号処理を統合的に実行する

    Args:
        labels: 処理対象のラベルリスト
        filter_non_parts: 機器符号以外のラベルをフィルタリングするかどうか
        validate_ref_designators: 未使用（機器符号妥当性チェック機能は v1.6.0 で削除）。
            `utils/extract_labels.py`（DXF-diff-manager とバイト一致コピーを維持する
            共有ファイル）がこの引数を渡し続けるため、シグネチャ互換のためだけに残す。
        debug: デバッグ情報を表示するかどうか

    Returns:
        dict: 処理結果を含む辞書
            - 'labels': 処理後のラベルリスト
            - 'filtered_count': フィルタリングで除外されたラベル数
            - 'invalid_ref_designators': 常に空リスト（機能削除済み、互換のため維持）
    """
    result = {
        'labels': labels.copy(),
        'filtered_count': 0,
        'invalid_ref_designators': []
    }

    # フィルタリング処理
    if filter_non_parts:
        filtered_labels, filtered_count = filter_non_circuit_symbols(labels, debug)
        result['labels'] = filtered_labels
        result['filtered_count'] = filtered_count

    return result
