"""產生 CiteGuard-Energy 的 Dify Workflow DSL（dsl/citeguard_workflow.yml）。

用法（repo 根目錄）：
    .venv\\Scripts\\python dsl\\build_dsl.py --dataset-id <Dify 知識庫 ID>

Code 節點的腳本放在 dsl/nodes/*.py（有單元測試 dsl/test_nodes.py），這裡只負責組裝。
知識庫文件名稱 → DOI 對照表從 kb_md/*.txt 開頭的 Title/DOI 行產生，存到 dsl/kb_doi_map.json。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DSL_DIR = ROOT / "dsl"
NODES_DIR = DSL_DIR / "nodes"

GROQ = {"provider": "langgenius/groq/groq", "name": "openai/gpt-oss-120b"}
# gemini-3.7-flash 免費額度每天只有 20 次，評測一輪就用完，改用 lite 版
GEMINI = {"provider": "langgenius/gemini/google", "name": "gemini-3.5-flash-lite"}
# grounding 一定要關：開了會用 Google 搜尋補資料，等於引用證據以外的來源
GEMINI_PARAMS = {"thinking_level": "Low", "grounding": False, "url_context": False}
EMBEDDING = {"provider": "langgenius/ollama/ollama", "name": "bge-m3:latest"}
OPENALEX_URL = "http://host.docker.internal:8000/openalex/search"
LLM_RETRY = {"retry_enabled": True, "max_retries": 2, "retry_interval": 2500}
# Gemini 尖峰時常回 503「需求過高」，報告節點多重試幾次、間隔拉長
REPORT_RETRY = {"retry_enabled": True, "max_retries": 3, "retry_interval": 5000}

DECOMPOSE_SYSTEM = """You are a search-query planner for an energy-engineering literature review tool \
(HVAC, chillers, data-center cooling, liquid/immersion cooling, AI data-center energy and water use).
The user question is usually in Traditional Chinese. Do NOT answer it.
Output ONLY one JSON object, no markdown fences, no explanation:
{"kb_query": "<one English query of 10-25 words capturing the core technical question, for semantic search over a local paper library>", "oa_query": "<3-6 English technical keywords for a scholarly search engine, no boolean operators, no quotes>"}
Translate Chinese terms into standard English technical terms, e.g. 冰水主機→chiller, 變頻→variable speed drive, \
液冷→liquid cooling, 浸沒式→immersion cooling, 冷板→cold plate, 節能→energy saving, 資料中心→data center, 用水→water consumption."""

REPORT_SYSTEM = """你是能源與空調節能領域的文獻調研助理。你只能根據使用者提供的〈證據〉撰寫繁體中文報告。

引用規則（最重要，違反就是捏造）：
1. 每一個含有事實、數據、比較或結論的句子，句尾都必須標註至少一個證據編號，格式為 [KB-1] 或 [OA-2]；同一句有多個來源時寫成 [KB-1][OA-2]。
2. 只能使用〈證據〉中實際出現的編號，絕對不可以自創編號，也不可以引用〈證據〉以外的論文、機構或數據。
3. 數字、單位與條件必須與證據原文一致；證據沒有寫的數字不要推算或補充。
4. 不同證據的條件不同（例如不同負載、氣候、系統規模）時要說明，不要把它們直接相加或混為一談。

證據不足的判斷（兩種情況要分清楚）：
- 【拒答】問題的核心技術、產品或名詞在〈證據〉中完全沒有出現（它可能根本不存在或是捏造的）：第一行只寫「【證據不足】」，接著用 2–3 句說明證據中找不到該技術、只找到哪些相關內容（照樣附編號），不得推測或編造數據。
- 【部分回答】核心技術在證據中有出現，只是缺少直接比較或完整數據：不要寫「【證據不足】」。照正常格式，用現有證據回答能回答的部分，並在「限制與建議」明確寫出缺少什麼（例如沒有同條件下的直接對照）。

報告格式（Markdown）：
## 結論摘要
2–4 句直接回答問題。
## 證據分析
分點整理，每點註明數據與適用條件。
## 限制與建議
證據的侷限（例如只有模擬、只有單一案例）與後續可查證的方向。

不要自己列參考文獻，系統會依你引用的編號自動附上。"""

REPORT_USER = """問題：{{#start.question#}}

〈證據〉
{{#evidence.evidence#}}"""


def node(node_id: str, x: int, y: int, data: dict) -> dict:
    return {
        "data": {"desc": "", "selected": False, **data},
        "height": 90, "width": 244, "id": node_id, "type": "custom",
        "position": {"x": x, "y": y}, "positionAbsolute": {"x": x, "y": y},
        "selected": False, "sourcePosition": "right", "targetPosition": "left",
    }


def edge(src: str, src_type: str, dst: str, dst_type: str) -> dict:
    return {
        "data": {"isInIteration": False, "isInLoop": False, "sourceType": src_type, "targetType": dst_type},
        "id": f"{src}-source-{dst}-target", "source": src, "sourceHandle": "source",
        "target": dst, "targetHandle": "target", "type": "custom", "zIndex": 0,
    }


def llm_data(title: str, model: dict, system: str, user: str, temperature: float, extra: dict | None = None,
             retry: dict | None = None) -> dict:
    return {
        "title": title, "type": "llm",
        "model": {**model, "mode": "chat", "completion_params": {"temperature": temperature, **(extra or {})}},
        "prompt_template": [{"role": "system", "text": system}, {"role": "user", "text": user}],
        "context": {"enabled": False, "variable_selector": []},
        "vision": {"enabled": False},
        "reasoning_format": "separated",
        "retry_config": retry or LLM_RETRY,
    }


def code_data(title: str, script: str, variables: list[tuple[str, list[str]]], outputs: dict[str, str]) -> dict:
    return {
        "title": title, "type": "code", "code_language": "python3", "code": script,
        "variables": [{"variable": v, "value_selector": sel} for v, sel in variables],
        "outputs": {k: {"type": t, "children": None} for k, t in outputs.items()},
    }


def load_doi_map() -> dict[str, str]:
    """從 kb_md/*.txt 開頭讀 Title/DOI；kb_md 不存在時（例如別人 clone repo）改讀已存的 json。"""
    cache = DSL_DIR / "kb_doi_map.json"
    kb_dir = ROOT / "kb_md"
    files = sorted(kb_dir.glob("*.txt")) if kb_dir.exists() else []
    if not files:
        return json.loads(cache.read_text(encoding="utf-8"))
    mapping = {}
    for f in files:
        head = f.read_text(encoding="utf-8").splitlines()[:2]
        m = re.match(r"DOI: https://doi\.org/(\S+)", head[1] if len(head) > 1 else "")
        mapping[f.stem] = m.group(1) if m else ""
    cache.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return mapping


def build(dataset_id: str) -> dict:
    doi_map = load_doi_map()
    evidence_code = (NODES_DIR / "build_evidence.py").read_text(encoding="utf-8").replace(
        "DOI_MAP = {}  # __DOI_MAP__",
        "DOI_MAP = " + json.dumps(doi_map, ensure_ascii=False, indent=4),
    )
    assert "__DOI_MAP__" not in evidence_code

    y = 260
    nodes = [
        node("start", 0, y, {
            "title": "開始", "type": "start",
            "variables": [{"label": "節能技術問題", "variable": "question", "type": "paragraph",
                           "required": True, "max_length": 500, "options": []}],
        }),
        node("decompose", 300, y, llm_data("問題拆解（Groq）", GROQ, DECOMPOSE_SYSTEM,
                                           "{{#start.question#}}", 0.1)),
        node("parse", 600, y, code_data(
            "解析查詢", (NODES_DIR / "parse_queries.py").read_text(encoding="utf-8"),
            [("llm_text", ["decompose", "text"]), ("question", ["start", "question"])],
            {"kb_query": "string", "oa_query": "string"})),
        node("kb", 900, y - 130, {
            "title": "知識庫檢索", "type": "knowledge-retrieval",
            "query_variable_selector": ["parse", "kb_query"],
            "dataset_ids": [dataset_id],
            "retrieval_mode": "multiple",
            "multiple_retrieval_config": {
                "top_k": 5, "score_threshold": None,
                "reranking_enable": False, "reranking_mode": "weighted_score",
                "weights": {
                    "vector_setting": {"vector_weight": 0.7, "embedding_provider_name": EMBEDDING["provider"],
                                       "embedding_model_name": EMBEDDING["name"]},
                    "keyword_setting": {"keyword_weight": 0.3},
                },
            },
            "metadata_filtering_mode": "disabled",
        }),
        node("openalex", 900, y + 130, {
            "title": "OpenAlex 檢索（FastAPI）", "type": "http-request", "method": "get",
            "url": OPENALEX_URL,
            "authorization": {"type": "no-auth", "config": None},
            "headers": "",
            "params": "query:{{#parse.oa_query#}}\nper_page:5\nfrom_year:2015",
            "body": {"type": "none", "data": []},
            "timeout": {"connect": 10, "read": 60, "write": 10},
            "retry_config": LLM_RETRY,
            # OpenAlex 掛掉時不要讓整條流程失敗：給空結果，報告只用知識庫證據
            "error_strategy": "default-value",
            "default_value": [
                {"key": "body", "type": "string", "value": "{\"results\": []}"},
                {"key": "status_code", "type": "number", "value": 0},
                {"key": "headers", "type": "object", "value": {}},
            ],
        }),
        node("evidence", 1200, y, code_data(
            "證據編號", evidence_code,
            [("kb_results", ["kb", "result"]), ("oa_body", ["openalex", "body"]),
             ("oa_status", ["openalex", "status_code"])],
            {"evidence": "string", "whitelist": "array[string]", "sources": "string",
             "kb_count": "number", "oa_count": "number",
             "retrieval_ok": "number", "warnings": "string"})),
        node("report", 1500, y, llm_data("報告生成（Gemini）", GEMINI, REPORT_SYSTEM, REPORT_USER, 0.3, GEMINI_PARAMS, REPORT_RETRY)),
        node("verify", 1800, y, code_data(
            "引用校驗", (NODES_DIR / "verify_citations.py").read_text(encoding="utf-8"),
            [("report", ["report", "text"]), ("whitelist", ["evidence", "whitelist"]),
             ("sources", ["evidence", "sources"]),
             ("warnings", ["evidence", "warnings"]), ("retrieval_ok", ["evidence", "retrieval_ok"])],
            {"report": "string", "valid_rate_before": "number", "valid_rate_after": "number",
             "invalid_citations": "number", "is_refusal": "number", "metrics": "string"})),
        node("end", 2100, y, {
            "title": "結束", "type": "end",
            "outputs": [
                {"variable": "report", "value_selector": ["verify", "report"], "value_type": "string"},
                {"variable": "metrics", "value_selector": ["verify", "metrics"], "value_type": "string"},
                {"variable": "valid_rate_before", "value_selector": ["verify", "valid_rate_before"], "value_type": "number"},
                {"variable": "valid_rate_after", "value_selector": ["verify", "valid_rate_after"], "value_type": "number"},
                {"variable": "is_refusal", "value_selector": ["verify", "is_refusal"], "value_type": "number"},
                {"variable": "raw_report", "value_selector": ["report", "text"], "value_type": "string"},
                {"variable": "kb_count", "value_selector": ["evidence", "kb_count"], "value_type": "number"},
                {"variable": "oa_count", "value_selector": ["evidence", "oa_count"], "value_type": "number"},
                {"variable": "retrieval_ok", "value_selector": ["evidence", "retrieval_ok"], "value_type": "number"},
                {"variable": "warnings", "value_selector": ["evidence", "warnings"], "value_type": "string"},
            ],
        }),
    ]
    edges = [
        edge("start", "start", "decompose", "llm"),
        edge("decompose", "llm", "parse", "code"),
        edge("parse", "code", "kb", "knowledge-retrieval"),
        edge("parse", "code", "openalex", "http-request"),
        edge("kb", "knowledge-retrieval", "evidence", "code"),
        edge("openalex", "http-request", "evidence", "code"),
        edge("evidence", "code", "report", "llm"),
        edge("report", "llm", "verify", "code"),
        edge("verify", "code", "end", "end"),
    ]
    return {
        "app": {
            "name": "CiteGuard-Energy",
            "description": "防捏造引用的空調／資料中心節能文獻調研工作流：知識庫 + OpenAlex 兩路檢索，報告每句附 [KB-x]/[OA-x]，自動剔除不存在的引用。",
            "icon": "🛡️", "icon_background": "#D1FADF", "mode": "workflow",
            "use_icon_as_answer_icon": False,
        },
        "dependencies": [],
        "kind": "app",
        "version": "0.3.1",
        "workflow": {
            "conversation_variables": [], "environment_variables": [],
            "features": {
                "file_upload": {"enabled": False}, "opening_statement": "",
                "retriever_resource": {"enabled": False}, "sensitive_word_avoidance": {"enabled": False},
                "speech_to_text": {"enabled": False}, "suggested_questions": [],
                "suggested_questions_after_answer": {"enabled": False}, "text_to_speech": {"enabled": False},
            },
            "graph": {"edges": edges, "nodes": nodes, "viewport": {"x": 0, "y": 0, "zoom": 0.6}},
        },
    }


class _LiteralDumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_LiteralDumper.add_representer(str, _str_presenter)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-id", required=True, help="Dify 知識庫 ID（知識庫網址中的 UUID）")
    ap.add_argument("--out", default=str(DSL_DIR / "citeguard_workflow.yml"))
    args = ap.parse_args()
    dsl = build(args.dataset_id)
    text = yaml.dump(dsl, Dumper=_LiteralDumper, allow_unicode=True, sort_keys=False, width=1000)
    pathlib.Path(args.out).write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(text)//1024} KB, {len(dsl['workflow']['graph']['nodes'])} nodes)")
