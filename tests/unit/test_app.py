"""app.py のうちStreamlit非依存で単体テストできる小さな純関数のテスト。

app.py はView層であり基本的にはブラウザでの動作確認に委ねるが、
dedupe_by_filename() はUI状態を持たない純関数のため単体テストする。
"""
from collections import namedtuple

from app import dedupe_by_filename

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
