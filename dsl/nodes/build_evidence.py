# Dify Code 節點「證據編號」：把知識庫與 OpenAlex 的結果編成 [KB-x] / [OA-x]，
# 並產生合法引用白名單。報告中不在白名單的編號會在「引用校驗」節點被剔除。
import json
import re

# 知識庫文件名稱 → DOI（由 dsl/build_dsl.py 從 kb_md/*.txt 的開頭自動產生並注入）
DOI_MAP = {}  # __DOI_MAP__

KB_MAX_CHARS = 1200
OA_MAX_CHARS = 1200


def _doc_title(name: str) -> str:
    return re.sub(r"\.(txt|md|pdf)$", "", (name or "").strip(), flags=re.I)


def _clean(text: str) -> str:
    # 去掉區塊開頭的 Title:/DOI: 行，避免 LLM 把它們當成內容
    lines = [l for l in (text or "").splitlines() if not l.startswith(("Title: ", "DOI: "))]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def main(kb_results: list, oa_body: str, oa_status: int) -> dict:
    # Dify 在知識庫檢索失敗（例如 Ollama 沒開）時不會報錯，只會回傳空陣列；
    # 混合檢索沒設分數門檻，正常情況一定有結果，所以空結果視為「檢索失敗」而不是「沒有證據」。
    warnings = []
    if not kb_results:
        warnings.append("知識庫檢索沒有回傳任何結果，可能是 Ollama（bge-m3）沒有啟動。")
    if oa_status != 200:
        warnings.append(f"OpenAlex 檢索失敗（HTTP {oa_status or '無回應'}），可能是 FastAPI 工具服務沒有啟動。")

    blocks = []
    whitelist = []
    sources = {}

    seen = set()
    for item in kb_results or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        title = _doc_title(meta.get("document_name") or item.get("title") or "")
        content = _clean(item.get("content") or "")
        key = meta.get("segment_id") or content[:200]
        if not content or key in seen:
            continue
        seen.add(key)
        sid = f"KB-{len([s for s in whitelist if s.startswith('KB-')]) + 1}"
        doi = DOI_MAP.get(title, "")
        whitelist.append(sid)
        sources[sid] = {"type": "KB", "title": title, "doi": doi,
                        "url": f"https://doi.org/{doi}" if doi else ""}
        blocks.append(f"[{sid}] 來源：{title}\n{content[:KB_MAX_CHARS]}")

    try:
        oa = json.loads(oa_body or "{}")
    except ValueError:
        oa = {}
    results = oa.get("results") if isinstance(oa, dict) else None
    for work in results or []:
        if not isinstance(work, dict):
            continue
        abstract = (work.get("abstract") or "").strip()
        title = (work.get("title") or "").strip()
        if not abstract or not title:
            continue
        sid = f"OA-{len([s for s in whitelist if s.startswith('OA-')]) + 1}"
        doi_url = work.get("doi") or ""
        whitelist.append(sid)
        sources[sid] = {"type": "OA", "title": title, "year": work.get("year"),
                        "venue": work.get("venue") or "",
                        "doi": re.sub(r"^https?://doi\.org/", "", doi_url),
                        "url": doi_url or work.get("url") or ""}
        head = f"[{sid}] 來源：{title}（{work.get('year') or '年份不詳'}，{work.get('venue') or '期刊不詳'}）"
        blocks.append(f"{head}\n{abstract[:OA_MAX_CHARS]}")

    evidence = "\n\n".join(blocks) if blocks else "（沒有找到任何證據）"
    return {
        "evidence": evidence,
        "whitelist": whitelist,
        "sources": json.dumps(sources, ensure_ascii=False),
        "kb_count": len([s for s in whitelist if s.startswith("KB-")]),
        "oa_count": len([s for s in whitelist if s.startswith("OA-")]),
        "retrieval_ok": 0 if not kb_results else 1,
        "warnings": "\n".join(warnings),
    }
