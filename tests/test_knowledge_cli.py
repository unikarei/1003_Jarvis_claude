"""knowledge_cli.py audit（--json の有無、DB を読めないとき）のテスト（T-1304）。"""

import json
import sqlite3

import pytest

import knowledge_cli
import knowledge_engine as ke

AUDIT = {
    "knowledge": 5,
    "duplicate_titles": [["k1", "k2", "k3"], ["k4", "k5"]],
    "duplicate_urls": [["k1", "k4"]],
    "stale": {"oarai-minpaku": 2, "piano": 1},
}


@pytest.fixture
def fixed_audit(monkeypatch, db_path):
    monkeypatch.setattr(ke, "audit", lambda *a, **k: AUDIT)


def test_audit_json(fixed_audit, capsys):
    assert knowledge_cli.main(["audit", "--json"]) == 0
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert data == AUDIT
    assert set(data) == {"knowledge", "duplicate_titles", "duplicate_urls", "stale"}
    assert isinstance(data["knowledge"], int)
    for key in ("duplicate_titles", "duplicate_urls"):
        assert all(isinstance(ids, list) and all(isinstance(i, str) for i in ids) for ids in data[key])
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in data["stale"].items())
    assert out == json.dumps(AUDIT, ensure_ascii=False, indent=2) + "\n"
    assert err == ""


def test_audit_json_real_db(db_path, capsys):
    """本物の audit()（テスト用の空の DB）の値と一致する。"""
    assert knowledge_cli.main(["audit", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == ke.audit()


def test_audit_text_unchanged(fixed_audit, capsys):
    assert knowledge_cli.main(["audit"]) == 0
    assert capsys.readouterr().out == (
        "Knowledge 5 件\n"
        "題名の重複: 2 組（最大 3 件）\n"
        "出典 URL の重複: 1 組\n"
        "鮮度の期限切れ: oarai-minpaku 2 件, piano 1 件\n"
    )


@pytest.mark.parametrize("argv", [["audit"], ["audit", "--json"]])
def test_audit_db_error(monkeypatch, db_path, capsys, argv):
    def broken(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(ke, "audit", broken)
    assert knowledge_cli.main(argv) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "エラー: unable to open database file\n"
