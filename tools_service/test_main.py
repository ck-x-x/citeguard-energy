"""tools_service 單元測試：全部使用假資料，不打網路。

執行（repo 根目錄）：python -m pytest tools_service -q
"""

from fastapi import HTTPException
from fastapi.testclient import TestClient

from tools_service import main
from tools_service.main import (
    build_params,
    format_response,
    reconstruct_abstract,
    truncate,
)

FAKE_WORK = {
    "id": "https://openalex.org/W123",
    "doi": "https://doi.org/10.1000/xyz",
    "title": "Liquid cooling of data centers",
    "publication_year": 2023,
    "primary_location": {
        "landing_page_url": "https://example.org/paper",
        "source": {"display_name": "Applied Thermal Engineering"},
    },
    "cited_by_count": 42,
    "abstract_inverted_index": {"cooling": [1, 4], "Liquid": [0], "is": [2], "efficient": [3]},
}


# --- 倒排索引還原 ---

def test_reconstruct_abstract_orders_by_position():
    assert reconstruct_abstract(FAKE_WORK["abstract_inverted_index"]) == "Liquid cooling is efficient cooling"


def test_reconstruct_abstract_empty():
    assert reconstruct_abstract(None) == ""
    assert reconstruct_abstract({}) == ""


def test_truncate():
    assert truncate("abc", 10) == "abc"
    out = truncate("a" * 2000, 1500)
    assert len(out) <= 1500 and out.endswith("…")


# --- 查詢參數 ---

def test_build_params_without_key_and_year():
    p = build_params("liquid cooling", 5, None, None)
    assert p["filter"] == "has_abstract:true"
    assert "api_key" not in p
    assert "mailto" not in p
    assert "abstract_inverted_index" in p["select"]


def test_build_params_with_year_and_key():
    p = build_params("liquid cooling", 3, 2018, "dummy-key")
    assert p["filter"] == "has_abstract:true,publication_year:>2017"
    assert p["api_key"] == "dummy-key"
    assert p["per_page"] == 3


# --- 回應整理 ---

def test_format_response():
    out = format_response("q", {"results": [FAKE_WORK, {"id": "https://openalex.org/W9", "primary_location": None}]})
    assert out["query"] == "q"
    assert out["count"] == 2
    r = out["results"][0]
    assert r == {
        "openalex_id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1000/xyz",
        "title": "Liquid cooling of data centers",
        "year": 2023,
        "venue": "Applied Thermal Engineering",
        "cited_by_count": 42,
        "abstract": "Liquid cooling is efficient cooling",
        "url": "https://doi.org/10.1000/xyz",
    }
    # 缺欄位時不應出錯，url 退回 openalex id
    r2 = out["results"][1]
    assert r2["venue"] is None and r2["abstract"] == "" and r2["url"] == "https://openalex.org/W9"


def test_format_response_empty():
    assert format_response("q", {}) == {"query": "q", "count": 0, "results": []}


# --- Endpoint（以假的 fetch_openalex 取代網路呼叫）---

def test_search_endpoint(monkeypatch):
    captured = {}

    async def fake_fetch(params):
        captured.update(params)
        return {"results": [FAKE_WORK]}

    monkeypatch.setattr(main, "fetch_openalex", fake_fetch)
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    client = TestClient(main.app)
    resp = client.get("/openalex/search", params={"query": "liquid cooling", "per_page": 2, "from_year": 2020})
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
    assert captured["filter"] == "has_abstract:true,publication_year:>2019"
    assert "api_key" not in captured


def test_search_endpoint_upstream_error(monkeypatch):
    async def fake_fetch(params):
        raise HTTPException(status_code=502, detail="OpenAlex 回傳錯誤（HTTP 500）")

    monkeypatch.setattr(main, "fetch_openalex", fake_fetch)
    resp = TestClient(main.app).get("/openalex/search", params={"query": "x"})
    assert resp.status_code == 502


def test_per_page_validation():
    client = TestClient(main.app)
    assert client.get("/openalex/search", params={"query": "x", "per_page": 11}).status_code == 422


def test_health_and_openapi():
    client = TestClient(main.app)
    assert client.get("/health").json() == {"status": "ok"}
    spec = client.get("/openapi.json").json()
    assert spec["servers"][0]["url"] == "http://host.docker.internal:8000"
    assert spec["paths"]["/openalex/search"]["get"]["operationId"] == "openalex_search"
