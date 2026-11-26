"""Code 節點腳本的單元測試（不需要 Dify）：.venv\\Scripts\\python -m pytest dsl"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "nodes"))

import build_evidence  # noqa: E402
import parse_queries  # noqa: E402
import verify_citations  # noqa: E402


# ---------- parse_queries ----------
def test_parse_json_ok():
    out = parse_queries.main('```json\n{"kb_query": "chiller control", "oa_query": "chiller VSD"}\n```', "q")
    assert out == {"kb_query": "chiller control", "oa_query": "chiller VSD"}


def test_parse_fallback_to_question():
    assert parse_queries.main("not json", "冰水主機") == {"kb_query": "冰水主機", "oa_query": "冰水主機"}
    assert parse_queries.main('<think>{"x":1}</think>{"kb_query": ""}', "q")["kb_query"] == "q"


# ---------- build_evidence ----------
KB = [
    {"title": "Paper A.txt", "content": "Title: Paper A\nDOI: x\nABSTRACT\nPUE reduced to 1.16.",
     "metadata": {"document_name": "Paper A.txt", "segment_id": "s1"}},
    {"title": "Paper A.txt", "content": "duplicate", "metadata": {"document_name": "Paper A.txt", "segment_id": "s1"}},
    {"title": "Paper B.txt", "content": "Immersion cooling saves 30%.", "metadata": {"document_name": "Paper B.txt", "segment_id": "s2"}},
]
OA = json.dumps({"results": [
    {"title": "OA paper", "abstract": "Liquid cooling abstract.", "year": 2023, "venue": "Energy", "doi": "https://doi.org/10.1/abc"},
    {"title": "No abstract", "abstract": "", "year": 2020},
]})


def test_build_evidence_numbering_and_dedupe():
    build_evidence.DOI_MAP = {"Paper A": "10.1016/a"}
    out = build_evidence.main(KB, OA, 200)
    assert out["whitelist"] == ["KB-1", "KB-2", "OA-1"]
    assert out["kb_count"] == 2 and out["oa_count"] == 1
    assert "Title:" not in out["evidence"] and "PUE reduced to 1.16." in out["evidence"]
    src = json.loads(out["sources"])
    assert src["KB-1"]["url"] == "https://doi.org/10.1016/a"
    assert src["OA-1"]["doi"] == "10.1/abc"


def test_build_evidence_bad_oa_body():
    out = build_evidence.main([], "OpenAlex 回傳錯誤", 503)
    assert out["whitelist"] == [] and out["evidence"] == "（沒有找到任何證據）"


# ---------- verify_citations ----------
SRC = json.dumps({"KB-1": {"type": "KB", "title": "Paper A", "url": "https://doi.org/10.1016/a"},
                  "OA-1": {"type": "OA", "title": "OA paper", "year": 2023, "url": "https://doi.org/10.1/abc"}})
WL = ["KB-1", "OA-1"]


def test_verify_removes_fabricated_and_keeps_valid():
    report = ("## 結論摘要\n"
              "浸沒式冷卻可將 PUE 降到 1.16 [KB-1]。提高室溫可省電 4–5% [KB-9]。"
              "液冷效率較高 [KB-1, OA-7]。\n"
              "- 冷板系統可節能 42.7% [OA-1][KB-3]\n"
              "- 這是捏造的條列 [OA-5]")
    out = verify_citations.main(report, WL, SRC)
    m = json.loads(out["metrics"])
    # KB-1, KB-9, KB-1, OA-7, OA-1, KB-3, OA-5 → 7 個，有效 3 個
    assert m["total_citations"] == 7 and m["valid_citations"] == 3 and m["invalid_citations"] == 4
    assert out["valid_rate_before"] == 0.4286 and out["valid_rate_after"] == 1.0
    assert m["removed_sentences"] == 2
    r = out["report"]
    assert "[KB-9]" not in r and "4–5%" not in r and "捏造的條列" not in r
    assert "液冷效率較高 [KB-1]。" in r
    assert "42.7% [OA-1]" in r
    assert "## 參考文獻" in r and "- [KB-1] Paper A https://doi.org/10.1016/a" in r and "[OA-1] OA paper（2023）" in r


def test_verify_citation_after_period_and_english():
    out = verify_citations.main("PUE dropped by 1.4%.[KB-1] Fake claim. [OA-3]\n", WL, SRC)
    assert "PUE dropped by 1.4%.[KB-1]" in out["report"] and "Fake claim" not in out["report"]


def test_verify_refusal_and_no_citations():
    out = verify_citations.main("【證據不足】\n證據中找不到「量子渦流冷卻」相關研究。", WL, SRC)
    assert out["is_refusal"] == 1 and out["valid_rate_before"] == 0.0
    assert "## 參考文獻" not in out["report"]


def test_verify_merges_same_paper_in_references():
    src = json.dumps({"KB-1": {"type": "KB", "title": "Paper A", "url": "u"},
                      "KB-4": {"type": "KB", "title": "Paper A", "url": "u"}})
    out = verify_citations.main("甲 [KB-1]。乙 [KB-4]。", ["KB-1", "KB-4"], src)
    assert "- [KB-1][KB-4] Paper A u" in out["report"]
    assert out["report"].count("Paper A") == 1


def test_verify_flags_empty_report():
    out = verify_citations.main("", WL, SRC)
    assert json.loads(out["metrics"])["empty_report"] == 1


def test_verify_refusal_after_heading():
    out = verify_citations.main("## 結論摘要\n【證據不足】證據中沒有相關數據 [KB-1]。", WL, SRC)
    assert out["is_refusal"] == 1


def test_build_evidence_flags_retrieval_failure():
    ok = build_evidence.main(KB, OA, 200)
    assert ok["retrieval_ok"] == 1 and ok["warnings"] == ""
    oa_down = build_evidence.main(KB, '{"results": []}', 0)
    assert oa_down["retrieval_ok"] == 1 and "FastAPI" in oa_down["warnings"]
    kb_down = build_evidence.main([], OA, 200)
    assert kb_down["retrieval_ok"] == 0 and "Ollama" in kb_down["warnings"]


def test_verify_prepends_warning_when_retrieval_failed():
    out = verify_citations.main("【證據不足】\n沒有證據。", [], "{}",
                                warnings="知識庫檢索沒有回傳任何結果，可能是 Ollama（bge-m3）沒有啟動。", retrieval_ok=0)
    assert out["report"].startswith("> ⚠️ 系統警告：知識庫檢索")
    assert "本次結果不可信" in out["report"]
    assert json.loads(out["metrics"])["retrieval_ok"] == 0
