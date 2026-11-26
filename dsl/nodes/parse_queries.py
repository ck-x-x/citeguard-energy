# Dify Code 節點「解析查詢」：把問題拆解 LLM 的 JSON 輸出轉成兩個檢索查詢。
# LLM 輸出壞掉時退回使用原始問題，確保後面兩路檢索一定有查詢可用。
import json
import re


def main(llm_text: str, question: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", llm_text or "", flags=re.S).strip()
    kb_query = ""
    oa_query = ""
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            kb_query = str(data.get("kb_query") or "").strip()
            oa_query = str(data.get("oa_query") or "").strip()
        except (ValueError, AttributeError):
            pass
    fallback = (question or "").strip()
    return {"kb_query": kb_query or fallback, "oa_query": oa_query or fallback}
