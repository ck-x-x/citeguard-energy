# Dify Code 節點「引用校驗」：確定性地檢查報告中的每個證據編號。
# - 不在白名單的編號（捏造引用）直接剔除
# - 句子原本有引用、但引用全部是捏造的 → 整句刪除（這句話沒有任何真實證據支持）
# - 沒有引用的句子（標題、轉折句）保留
# - 輸出校驗前／校驗後的有效引用率，跑一次就有 Baseline 對比
import json
import re

# 方括號裡含 KB-n / OA-n 的標記，例如 [KB-1]、[KB-1, OA-2]、[OA-3；KB-2]
TAG_RE = re.compile(r"\[([^\[\]\n]*?(?:KB|OA)\s*[-–]\s*\d+[^\[\]\n]*?)\]")
ID_RE = re.compile(r"(KB|OA)\s*[-–]\s*(\d+)")
# 句子：到中英文句末標點為止，並吃掉緊接在後面的引用標記（「…。[KB-1]」）
SENT_RE = re.compile(r".*?(?:[。！？!?]|\.(?=\s|$|\[))(?:\s*\[[^\[\]\n]*\])*|.+$")
REFUSAL_MARK = "【證據不足】"


def _process_sentence(sentence: str, allowed: set, stats: dict, cited: list) -> str:
    had_ids = False
    kept_any = False

    def fix_tag(m):
        nonlocal had_ids, kept_any
        ids = [f"{p}-{n}" for p, n in ID_RE.findall(m.group(1))]
        valid = []
        for i in ids:
            had_ids = True
            stats["total"] += 1
            if i in allowed:
                stats["valid"] += 1
                if i not in valid:
                    valid.append(i)
                if i not in cited:
                    cited.append(i)
            else:
                stats["invalid"] += 1
        if valid:
            kept_any = True
        return "".join(f"[{i}]" for i in valid)

    new = TAG_RE.sub(fix_tag, sentence)
    if had_ids and not kept_any:
        stats["removed_sentences"] += 1
        return ""
    return re.sub(r"[ \t]+([。，、；：！？.,;:!?])", r"\1", new)


def main(report: str, whitelist: list, sources: str, warnings: str = "", retrieval_ok: int = 1) -> dict:
    allowed = set(whitelist or [])
    try:
        src = json.loads(sources or "{}")
    except ValueError:
        src = {}
    stats = {"total": 0, "valid": 0, "invalid": 0, "removed_sentences": 0}
    cited = []

    text = re.sub(r"<think>.*?</think>", "", report or "", flags=re.S).strip()
    out_lines = []
    for line in text.splitlines():
        if not line.strip():
            out_lines.append(line)
            continue
        parts = [m.group(0) for m in SENT_RE.finditer(line) if m.group(0)]
        new_line = "".join(_process_sentence(p, allowed, stats, cited) for p in parts)
        # 整行都被刪掉的條列項目／段落就不留空殼
        if new_line.strip() in ("", "-", "*", "•") or re.fullmatch(r"\s*([-*•]|\d+\.)\s*", new_line):
            continue
        out_lines.append(new_line.rstrip())
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()

    if cited:
        # 同一篇論文的多個段落（例如 KB-1、KB-4）合併成一行
        groups = {}
        for i in sorted(cited, key=lambda x: (x[:2] != "KB", int(x[3:]))):
            s = src.get(i, {})
            key = (s.get("title") or i, s.get("url") or "")
            groups.setdefault(key, {"ids": [], "src": s})["ids"].append(i)
        refs = ["", "## 參考文獻"]
        for (title, url), g in groups.items():
            s = g["src"]
            line = "- " + "".join(f"[{i}]" for i in g["ids"]) + f" {s.get('title') or '（標題不詳）'}"
            if s.get("type") == "OA" and s.get("year"):
                line += f"（{s['year']}）"
            if url:
                line += f" {url}"
            refs.append(line)
        body += "\n" + "\n".join(refs)

    # 檢索失敗時在報告最上方明確警告，避免讀者把「服務沒開」誤以為「查無證據」
    if warnings:
        notice = "\n".join(f"> ⚠️ 系統警告：{w}" for w in warnings.splitlines() if w.strip())
        if not retrieval_ok:
            notice += "\n> 本次結果不可信，請啟動服務後重新執行。"
        body = notice + "\n\n" + body

    total = stats["total"]
    kept = stats["valid"]
    metrics = {
        "total_citations": total,
        "valid_citations": kept,
        "invalid_citations": stats["invalid"],
        "valid_rate_before": round(kept / total, 4) if total else 0.0,
        "valid_rate_after": 1.0 if kept else 0.0,
        "removed_sentences": stats["removed_sentences"],
        "cited_sources": len(cited),
        # LLM 有時會先寫「## 結論摘要」標題再寫【證據不足】，所以看開頭一段而不是只看第一個字
        "is_refusal": 1 if REFUSAL_MARK in text[:300] else 0,
        # 報告生成失敗（例如 LLM 額度用完）時報告是空的，評測要算失敗而不是「沒有捏造」
        "empty_report": 0 if text else 1,
        "retrieval_ok": 1 if retrieval_ok else 0,
        "warnings": warnings or "",
    }
    return {
        "report": body,
        "valid_rate_before": metrics["valid_rate_before"],
        "valid_rate_after": metrics["valid_rate_after"],
        "invalid_citations": metrics["invalid_citations"],
        "is_refusal": metrics["is_refusal"],
        "metrics": json.dumps(metrics, ensure_ascii=False),
    }
