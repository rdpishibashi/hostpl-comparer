"""app.py のうちStreamlit非依存で単体テストできる小さな純関数のテスト。

app.py はView層であり基本的にはブラウザでの動作確認に委ねるが、
dedupe_by_filename()・_filter_no_expansion() はUI状態を持たない純関数のため
単体テストする。
"""
from collections import namedtuple

from app import _filter_no_expansion, dedupe_by_filename

_FakeFile = namedtuple("_FakeFile", ["name"])


def test_dedupe_by_filename_keeps_first_occurrence_of_duplicates():
    files = [_FakeFile("EE0001-000-01A.dxf"), _FakeFile("EE0001-000-01A.dxf"), _FakeFile("EE0002-000-01A.dxf")]

    unique, duplicate_count = dedupe_by_filename(files)

    assert [f.name for f in unique] == ["EE0001-000-01A.dxf", "EE0002-000-01A.dxf"]
    assert duplicate_count == 1


def test_dedupe_by_filename_no_duplicates():
    files = [_FakeFile("a.xlsx"), _FakeFile("b.xlsx")]

    unique, duplicate_count = dedupe_by_filename(files)

    assert unique == files
    assert duplicate_count == 0


def test_dedupe_by_filename_empty_list():
    unique, duplicate_count = dedupe_by_filename([])
    assert unique == []
    assert duplicate_count == 0


# --- _filter_no_expansion（2026-09-08、ユーザー指摘: 別ファイルで部品展開済みの
# 図番が「部品展開なし」にも重複表示されるのは冗長） ---

def test_filter_no_expansion_removes_drawing_number_expanded_in_another_file():
    """実データで確認した回帰: EE6661-000-05Aは自身のファイルで展開されるが、
    EE6313-000-01Cのファイルには参照行としても現れる。後者からは除く。"""
    no_expansion_by_file = [
        ("EE6313-000-01C.xlsx", ["EE5283-000-21G", "EE6661-000-05A"]),
    ]
    ulkes_map = {"EE6661-000-05A": ["R1"]}

    result = _filter_no_expansion(no_expansion_by_file, ulkes_map)

    assert result == [("EE6313-000-01C.xlsx", ["EE5283-000-21G"])]


def test_filter_no_expansion_drops_file_entirely_if_all_entries_removed():
    no_expansion_by_file = [("a.xlsx", ["EE0001-000-01A"])]
    ulkes_map = {"EE0001-000-01A": ["R1"]}

    result = _filter_no_expansion(no_expansion_by_file, ulkes_map)

    assert result == []


def test_filter_no_expansion_keeps_entries_not_expanded_anywhere():
    no_expansion_by_file = [("a.xlsx", ["EE0001-000-01A"])]
    ulkes_map = {}

    result = _filter_no_expansion(no_expansion_by_file, ulkes_map)

    assert result == [("a.xlsx", ["EE0001-000-01A"])]
