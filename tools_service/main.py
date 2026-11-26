"""CiteGuard-Energy 工具服務（FastAPI）

提供給 Dify Workflow 呼叫的自訂工具：查詢 OpenAlex 學術文獻並整理成精簡 JSON。

啟動方式（在 repo 根目錄、已啟用 .venv）：
    uvicorn tools_service.main:app --host 0.0.0.0 --port 8000

Dify 跑在 Docker 中，透過 http://host.docker.internal:8000 呼叫本服務；
在 Dify「自訂工具」匯入 http://localhost:8000/openapi.json 即可（servers 已預設為 host.docker.internal）。

設定：repo 根目錄的 .env 中的 OPENALEX_API_KEY（可留空，留空時請求不帶 api_key）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

# 從 repo 根目錄讀取 .env（本檔位於 <repo>/tools_service/main.py）
REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_TIMEOUT_SECONDS = 20.0
ABSTRACT_MAX_CHARS = 1500
SELECT_FIELDS = "id,doi,title,publication_year,primary_location,cited_by_count,abstract_inverted_index"

logger = logging.getLogger("citeguard.tools")

app = FastAPI(
    title="CiteGuard-Energy Tools",
    description="CiteGuard-Energy 的外部工具服務：查詢 OpenAlex 學術文獻，回傳含摘要的精簡結果，供 Dify Workflow 產生 [OA-x] 證據編號。",
    version="0.1.0",
    servers=[{"url": "http://host.docker.internal:8000", "description": "Dify（Docker）呼叫 Windows 主機上的本服務"}],
)


# ---------------------------------------------------------------------------
# 純函式（方便單元測試）
# ---------------------------------------------------------------------------

def reconstruct_abstract(inverted_index: Optional[dict[str, list[int]]]) -> str:
    """把 OpenAlex 的 abstract_inverted_index（字 → 位置清單）依位置排序還原成摘要文字。"""
    if not inverted_index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted_index.items():
        for i in idxs or []:
            positions.append((i, word))
    positions.sort(key=lambda p: p[0])
    return " ".join(word for _, word in positions)


def truncate(text: str, limit: int = ABSTRACT_MAX_CHARS) -> str:
    """把文字截到 limit 字元以內，被截斷時以「…」結尾。"""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_params(query: str, per_page: int, from_year: Optional[int], api_key: Optional[str]) -> dict[str, Any]:
    """組出 OpenAlex /works 的查詢參數。"""
    filters = ["has_abstract:true"]
    if from_year is not None:
        filters.append(f"publication_year:>{from_year - 1}")
    params: dict[str, Any] = {
        "search": query,
        "filter": ",".join(filters),
        "per_page": per_page,
        "select": SELECT_FIELDS,
    }
    if api_key:
        params["api_key"] = api_key
    return params


def format_work(work: dict[str, Any]) -> dict[str, Any]:
    """把單筆 OpenAlex work 整理成給 LLM 看的精簡格式。"""
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    doi = work.get("doi")
    url = doi or location.get("landing_page_url") or work.get("id")
    return {
        "openalex_id": work.get("id"),
        "doi": doi,
        "title": work.get("title"),
        "year": work.get("publication_year"),
        "venue": source.get("display_name"),
        "cited_by_count": work.get("cited_by_count"),
        "abstract": truncate(reconstruct_abstract(work.get("abstract_inverted_index"))),
        "url": url,
    }


def format_response(query: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把 OpenAlex 回應整理成本服務的回傳格式。"""
    results = [format_work(w) for w in payload.get("results") or []]
    return {"query": query, "count": len(results), "results": results}


# ---------------------------------------------------------------------------
# 對外呼叫
# ---------------------------------------------------------------------------

async def fetch_openalex(params: dict[str, Any]) -> dict[str, Any]:
    """呼叫 OpenAlex；錯誤時丟出 HTTPException(502)，訊息中不含 api_key。"""
    try:
        async with httpx.AsyncClient(timeout=OPENALEX_TIMEOUT_SECONDS) as client:
            resp = await client.get(OPENALEX_WORKS_URL, params=params)
    except httpx.TimeoutException:
        logger.warning("OpenAlex 請求逾時")
        raise HTTPException(status_code=502, detail="OpenAlex 請求逾時，請稍後再試")
    except httpx.HTTPError as exc:
        # 只記錄例外類型，避免把含 api_key 的 URL 寫進 log
        logger.warning("OpenAlex 連線失敗：%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="無法連線到 OpenAlex")

    if resp.status_code != 200:
        logger.warning("OpenAlex 回傳錯誤狀態碼 %s", resp.status_code)
        hint = "（可能需要在 .env 設定 OPENALEX_API_KEY）" if resp.status_code in (401, 403, 429) else ""
        raise HTTPException(status_code=502, detail=f"OpenAlex 回傳錯誤（HTTP {resp.status_code}）{hint}")

    try:
        return resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="OpenAlex 回應不是有效的 JSON")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    operation_id="health",
    summary="健康檢查",
    description="確認工具服務是否正常運作，回傳 {\"status\": \"ok\"}。",
)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/openalex/search",
    operation_id="openalex_search",
    summary="搜尋 OpenAlex 學術文獻",
    description=(
        "Search scholarly works on OpenAlex by English keywords and return papers that have abstracts. "
        "Each result includes openalex_id, doi, title, year, venue, cited_by_count, abstract (truncated to ~1500 chars) and url. "
        "Use concise English technical keywords (e.g. 'data center immersion cooling PUE'). "
        "以英文關鍵字搜尋 OpenAlex 學術文獻，只回傳有摘要的論文，供報告引用為 [OA-x] 證據。"
    ),
)
async def openalex_search(
    query: str = Query(
        ...,
        min_length=1,
        description="English search keywords, e.g. 'data center liquid cooling'. 英文搜尋關鍵字。",
    ),
    per_page: int = Query(
        5,
        ge=1,
        le=10,
        description="Number of papers to return (1-10, default 5). 回傳論文數量。",
    ),
    from_year: Optional[int] = Query(
        None,
        ge=1900,
        le=2100,
        description="Optional. Only return papers published in or after this year, e.g. 2018. 只回傳此年份（含）之後的論文。",
    ),
) -> dict[str, Any]:
    params = build_params(query, per_page, from_year, os.getenv("OPENALEX_API_KEY") or None)
    payload = await fetch_openalex(params)
    return format_response(query, payload)
