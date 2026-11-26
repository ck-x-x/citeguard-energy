"""CiteGuard-Energy 評測腳本：逐題呼叫 Dify Workflow API（/v1/workflows/run），計算引用與拒答指標。

用法（repo 根目錄，FastAPI 與 Dify 都要先啟動）：
    .venv\\Scripts\\python eval\\run_eval.py                # 全部 10 題
    .venv\\Scripts\\python eval\\run_eval.py --ids N1 T1    # 只跑指定題目

需要 .env 中的 DIFY_API_BASE（預設 http://localhost/v1）與 DIFY_WORKFLOW_API_KEY（Workflow 發布後在「API 存取」建立）。
結果輸出到 eval/results/<時間>/：results.json、summary.md、reports/<題號>.md

指標：
- 有效引用率（校驗前）：報告中所有 [KB-x]/[OA-x] 有多少真的在證據白名單內 → 等於「沒有 CiteGuard 時」的 baseline
- 有效引用率（校驗後）：引用校驗節點剔除捏造引用後的比例
- 正常題回答率：7 題正常題中沒有被判為【證據不足】的比例
- 陷阱題拒答率：3 題捏造技術中正確回答【證據不足】的比例
- 來源命中率：正常題的報告是否引用到預期的知識庫論文
- 關鍵數據命中率：正常題的報告是否包含預期的關鍵數字
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import time

import httpx
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
REFUSAL_MARK = "【證據不足】"
load_dotenv(ROOT / ".env")


def preflight(dify_base: str) -> list[str]:
    """開跑前確認 Dify、FastAPI、Ollama 都活著；任何一個沒開，整輪評測都會被汙染。"""
    checks = [
        ("Dify API", dify_base.rstrip("/").rsplit("/v1", 1)[0] + "/console/api/ping"),
        ("FastAPI 工具服務（uvicorn tools_service.main:app --port 8000）", "http://localhost:8000/health"),
        ("Ollama（bge-m3 embedding）", "http://localhost:11434"),
    ]
    problems = []
    for name, url in checks:
        try:
            if httpx.get(url, timeout=5.0).status_code >= 500:
                problems.append(f"{name} 回應錯誤：{url}")
        except httpx.HTTPError:
            problems.append(f"{name} 連不上：{url}")
    return problems


def call_workflow(client: httpx.Client, base: str, key: str, question: str) -> dict:
    resp = client.post(
        f"{base.rstrip('/')}/workflows/run",
        headers={"Authorization": f"Bearer {key}"},
        json={"inputs": {"question": question}, "response_mode": "blocking", "user": "citeguard-eval"},
    )
    resp.raise_for_status()
    return resp.json()


def fact_hit(report: str, fact) -> bool:
    options = fact if isinstance(fact, list) else [fact]
    return any(o in report for o in options)


def references_section(report: str) -> str:
    i = report.find("## 參考文獻")
    return report[i:] if i >= 0 else ""


def score(item: dict, outputs: dict) -> dict:
    report = outputs.get("report") or ""
    m = json.loads(outputs.get("metrics") or "{}")
    refs = references_section(report)
    row = {
        "id": item["id"], "type": item["type"], "question": item["question"],
        "status": "succeeded",
        # 以報告內容再判斷一次：【證據不足】可能出現在「## 結論摘要」標題之後
        "is_refusal": int(bool(m.get("is_refusal", 0)) or REFUSAL_MARK in report[:300]),
        "total_citations": m.get("total_citations", 0),
        "valid_citations": m.get("valid_citations", 0),
        "invalid_citations": m.get("invalid_citations", 0),
        "removed_sentences": m.get("removed_sentences", 0),
        "valid_rate_before": m.get("valid_rate_before", 0.0),
        "valid_rate_after": m.get("valid_rate_after", 0.0),
        "kb_count": outputs.get("kb_count"), "oa_count": outputs.get("oa_count"),
    }
    if item["type"] == "normal":
        exp = item.get("expected_sources") or []
        row["source_hit"] = int(any(e.lower() in refs.lower() for e in exp)) if exp else None
        facts = item.get("key_facts") or []
        row["key_fact_hit_rate"] = round(sum(fact_hit(report, f) for f in facts) / len(facts), 4) if facts else None
        row["correct"] = int(row["is_refusal"] == 0)
    else:
        row["correct"] = int(row["is_refusal"] == 1)
    return row


def pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["status"] == "succeeded"]
    normal = [r for r in ok if r["type"] == "normal"]
    trap = [r for r in ok if r["type"] == "trap"]
    total = sum(r["total_citations"] for r in ok)
    valid = sum(r["valid_citations"] for r in ok)
    src = [r["source_hit"] for r in normal if r.get("source_hit") is not None]
    facts = [r["key_fact_hit_rate"] for r in normal if r.get("key_fact_hit_rate") is not None]
    return {
        "questions": len(rows), "succeeded": len(ok),
        "total_citations": total, "valid_citations": valid,
        "invalid_citations_removed": sum(r["invalid_citations"] for r in ok),
        "removed_sentences": sum(r["removed_sentences"] for r in ok),
        "valid_rate_before": round(valid / total, 4) if total else None,
        "valid_rate_after": 1.0 if valid else None,
        "normal_answer_rate": round(sum(r["correct"] for r in normal) / len(normal), 4) if normal else None,
        "trap_refusal_rate": round(sum(r["correct"] for r in trap) / len(trap), 4) if trap else None,
        "source_hit_rate": round(sum(src) / len(src), 4) if src else None,
        "key_fact_hit_rate": round(sum(facts) / len(facts), 4) if facts else None,
        "avg_elapsed_s": round(sum(r["elapsed_s"] for r in ok) / len(ok), 1) if ok else None,
    }


def write_summary(path: pathlib.Path, s: dict, rows: list[dict], started: str) -> None:
    lines = [
        f"# CiteGuard-Energy 評測結果（{started}）", "",
        "| 指標 | 數值 |", "|---|---|",
        f"| 成功執行 | {s['succeeded']} / {s['questions']} |",
        f"| 引用總數 | {s['total_citations']} |",
        f"| 有效引用率（校驗前，baseline） | {pct(s['valid_rate_before'])} |",
        f"| 有效引用率（校驗後） | {pct(s['valid_rate_after'])} |",
        f"| 剔除的捏造引用 / 刪除的句子 | {s['invalid_citations_removed']} / {s['removed_sentences']} |",
        f"| 正常題回答率 | {pct(s['normal_answer_rate'])} |",
        f"| 陷阱題拒答率 | {pct(s['trap_refusal_rate'])} |",
        f"| 來源命中率（正常題） | {pct(s['source_hit_rate'])} |",
        f"| 關鍵數據命中率（正常題） | {pct(s['key_fact_hit_rate'])} |",
        f"| 平均耗時 | {s['avg_elapsed_s']} 秒 |",
        "", "## 逐題結果", "",
        "| 題號 | 類型 | 結果 | 引用（有效/總數） | 校驗前有效率 | 來源命中 | 關鍵數據 | KB/OA 證據 | 耗時 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r["status"] != "succeeded":
            lines.append(f"| {r['id']} | {r['type']} | ❌ {r.get('error', '')[:60]} | | | | | | |")
            continue
        verdict = ("✅ 拒答" if r["is_refusal"] else "❌ 未拒答") if r["type"] == "trap" else \
                  ("✅ 回答" if not r["is_refusal"] else "⚠️ 拒答")
        src = {1: "✅", 0: "❌"}.get(r.get("source_hit"), "—")
        lines.append(
            f"| {r['id']} | {r['type']} | {verdict} | {r['valid_citations']}/{r['total_citations']} | "
            f"{pct(r['valid_rate_before'])} | {src} | {pct(r.get('key_fact_hit_rate'))} | "
            f"{r['kb_count']}/{r['oa_count']} | {r['elapsed_s']} 秒 |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def rescore(out_dir: pathlib.Path, dataset: pathlib.Path) -> int:
    items = {q["id"]: q for q in json.loads(dataset.read_text(encoding="utf-8"))["questions"]}
    data = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    rows = []
    for old in data["rows"]:
        if old["status"] != "succeeded":
            rows.append(old)
            continue
        text = (out_dir / "reports" / f"{old['id']}.md").read_text(encoding="utf-8")
        report = text.split("\n\n", 1)[1] if "\n\n" in text else text
        metrics = {k: old[k] for k in ("total_citations", "valid_citations", "invalid_citations",
                                       "removed_sentences", "valid_rate_before", "valid_rate_after", "is_refusal")}
        row = score(items[old["id"]], {"report": report, "metrics": json.dumps(metrics),
                                       "kb_count": old["kb_count"], "oa_count": old["oa_count"]})
        row["elapsed_s"] = old["elapsed_s"]
        row["run_status"] = old.get("run_status")
        rows.append(row)
    summary = summarize(rows)
    data.update({"summary": summary, "rows": rows, "rescored_at": dt.datetime.now().isoformat(timespec="seconds")})
    (out_dir / "results.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(out_dir / "summary.md", summary, rows, data["started"])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "eval" / "golden_dataset.json"))
    ap.add_argument("--ids", nargs="*", help="只跑指定題號，例如 N1 T1")
    ap.add_argument("--delay", type=float, default=8.0, help="題目間隔秒數，避免 Groq/Gemini 免費額度 429")
    ap.add_argument("--rescore", help="用 eval/results/<時間> 已存的報告重新計分，不呼叫 Dify")
    args = ap.parse_args()

    if args.rescore:
        return rescore(pathlib.Path(args.rescore), pathlib.Path(args.dataset))

    base = os.getenv("DIFY_API_BASE") or "http://localhost/v1"
    key = os.getenv("DIFY_WORKFLOW_API_KEY")
    if not key:
        print("請在 .env 設定 DIFY_WORKFLOW_API_KEY（Workflow 發布後在「API 存取」建立）", file=sys.stderr)
        return 1

    problems = preflight(base)
    if problems:
        print("開跑前檢查失敗，請先啟動以下服務再重跑：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    items = json.loads(pathlib.Path(args.dataset).read_text(encoding="utf-8"))["questions"]
    if args.ids:
        items = [q for q in items if q["id"] in set(args.ids)]

    started = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "eval" / "results" / started
    (out_dir / "reports").mkdir(parents=True, exist_ok=True)

    rows = []
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
        for n, item in enumerate(items, 1):
            print(f"[{n}/{len(items)}] {item['id']} {item['question'][:40]}…", flush=True)
            t0 = time.time()
            try:
                data = call_workflow(client, base, key, item["question"])["data"]
                # partial-succeeded：有節點出錯但走了容錯分支（例如 OpenAlex 失敗改用空結果），報告仍可用
                if data.get("status") not in ("succeeded", "partial-succeeded"):
                    raise RuntimeError(data.get("error") or data.get("status"))
                outputs = data.get("outputs") or {}
                if json.loads(outputs.get("metrics") or "{}").get("empty_report") or not outputs.get("report"):
                    raise RuntimeError(f"報告是空的（LLM 可能失敗）：{data.get('error') or data.get('status')}")
                # 檢索失敗（服務沒開）時報告會像「證據不足」，不能算進拒答率
                if outputs.get("retrieval_ok") == 0:
                    raise RuntimeError(f"檢索失敗：{outputs.get('warnings') or '知識庫沒有回傳結果'}")
                row = score(item, outputs)
                row["run_status"] = data.get("status")
                (out_dir / "reports" / f"{item['id']}.md").write_text(
                    f"# {item['id']}：{item['question']}\n\n{outputs.get('report', '')}\n", encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 — 單題失敗不影響其他題
                row = {"id": item["id"], "type": item["type"], "question": item["question"],
                       "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            row["elapsed_s"] = round(time.time() - t0, 1)
            rows.append(row)
            print(f"    → {row['status']}  {row.get('valid_citations', '-')}/{row.get('total_citations', '-')} 引用有效"
                  f"  拒答={row.get('is_refusal', '-')}  {row['elapsed_s']}s", flush=True)
            if n < len(items):
                time.sleep(args.delay)

    summary = summarize(rows)
    (out_dir / "results.json").write_text(
        json.dumps({"started": started, "summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    write_summary(out_dir / "summary.md", summary, rows, started)
    print(f"\n結果：{out_dir / 'summary.md'}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
