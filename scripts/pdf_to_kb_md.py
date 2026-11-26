"""把論文 PDF 轉成給 Dify 知識庫用的乾淨 Markdown。

用法: python -I clean_pdf.py <pdf資料夾> <輸出資料夾>
- 刪掉 CRediT / Declaration / References 之後的內容
- 刪掉重複出現的頁首頁尾、頁碼、版權與投稿資訊
- 刪掉表格殘渣（短且多數字的行）
- 刪掉文中數字引用 [12]、[3,4]、[5–8]，避免和 [KB-x] 混淆
- 開頭加上標題與 DOI，另產生 dois.txt
"""
import collections
import math
import pathlib
import re
import subprocess
import sys

END_MARKERS = re.compile(
    r"^(CRediT authorship|Declaration of competing interest|DECLARATION OF INTERESTS"
    r"|ACKNOWLEDG|Acknowledg|RESOURCE AVAILABILITY|AUTHOR CONTRIBUTIONS|Data availability)"
)
REF_HEADING = re.compile(r"^(References|REFERENCES)\s*$")
DOI_RE = re.compile(r"doi\.org/(10\.\d{4,9}/[^\s,;]+?)[.,;]?(?:\s|$)", re.I)
JUNK_LINE = re.compile(
    r"(Contents lists available at|journal homepage|Corresponding author|E-mail address"
    r"|^https?://doi\.org/.*Received|All rights reserved|©|Elsevier Ltd|Published by Elsevier"
    r"|This is an open access article|creativecommons\.org|^Received \d|Available online)",
    re.I,
)
HEADING = re.compile(r"^(\d+(\.\d+)*\.?\s+[A-Z][^.]{0,90}|Abstract|ABSTRACT|Introduction|INTRODUCTION"
                     r"|Conclusions?|CONCLUSIONS?|Discussion|DISCUSSION|Results|RESULTS|Nomenclature)\s*$")
CITE_RE = re.compile(r"\s?\[\d+(?:\s?[,–\-]\s?\d+)*\]")
# Cell/Joule 期刊的上標引用，例如 "demand.12" "grid,34–36"
SUP_CITE_RE = re.compile(r"(?<=[A-Za-z)][.,;])\d{1,3}(?:[,–\-]\d{1,3})*(?=\s|$)")
# 圖說："Fig. 3. Internal structure..."（編號後有句點）；表名："Table 1 Performance parameters..."（短、沒有動詞）
# 正文像 "Fig. 14 shows ..."、"Table 6 presents ..." 要保留
CAPTION_RE = re.compile(r"^(Fig\.|Figure)\s*\d+[A-Za-z]?\.\s")
TABLE_TITLE_RE = re.compile(r"^Table\s*\d+\.?\s")
BODY_VERB_RE = re.compile(r"\b(shows?|presents?|lists?|summari[sz]es?|compares?|indicates?|illustrates?|provides?|reports?|gives?|depicts?|demonstrates?|displays?|reveals?|is|are|was|were)\b")


def pdf_to_text(pdf: pathlib.Path) -> str:
    out = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"],
                         capture_output=True, check=True)
    return out.stdout.decode("utf-8", errors="replace")


def cut_end_matter(lines):
    n = len(lines)
    start = int(n * 0.5)
    for i in range(start, n):
        if END_MARKERS.match(lines[i].strip()):
            return lines[:i]
    for i in range(n - 1, start, -1):
        if REF_HEADING.match(lines[i].strip()):
            return lines[:i]
    return lines


def is_table_junk(line: str) -> bool:
    words = line.split()
    if HEADING.match(line) or line.startswith(("Fig.", "Figure", "Table")):
        return False
    if len(words) < 5:
        return True
    alpha = sum(1 for w in words if re.fullmatch(r"[A-Za-z][A-Za-z\-’']+[.,;:]?", w))
    return alpha / len(words) < 0.5


def clean(text: str):
    text = text.replace(chr(0xAD), "").replace("\r", "")  # 軟連字號
    pages = text.split("\f")
    # 頁首頁尾：在 3 頁以上重複出現的短行
    counter = collections.Counter()
    for p in pages:
        for l in set(x.strip() for x in p.split("\n")):
            if l and len(l) < 120:
                counter[l] += 1
    repeated = {l for l, c in counter.items() if c >= 3}

    lines = [l.strip() for l in text.replace("\f", "\n").split("\n")]
    lines = cut_end_matter(lines)

    paras = []
    for l in lines:
        if not l or l in repeated or re.fullmatch(r"\d{1,3}", l) or JUNK_LINE.search(l):
            continue
        # 圖表標題：圖表本體已被刪掉，單獨的標題不能當證據
        if is_table_junk(l) or CAPTION_RE.match(l) or (
                TABLE_TITLE_RE.match(l) and len(l) < 200 and not BODY_VERB_RE.search(l)):
            continue
        l = SUP_CITE_RE.sub("", CITE_RE.sub("", l)).replace("��", "'").replace("�", "")
        heading = bool(HEADING.match(l))
        # 跨頁/跨欄斷掉的句子：前段沒有句尾標點、這行小寫開頭 → 接回去
        if paras and not heading and not paras[-1].startswith("## ") \
                and not re.search(r"[.!?:)]$", paras[-1]) and re.match(r"[a-z(\d]", l):
            paras[-1] = paras[-1].rstrip("-") + ("" if paras[-1].endswith("-") else " ") + l
            continue
        paras.append(f"## {l}" if heading else l)
    return paras


START_RE = re.compile(r"^## (ABSTRACT|Abstract|SUMMARY|Summary|1\.? Introduction|INTRODUCTION)\b")
KEYWORDS_RE = re.compile(r"^(Keywords?|KEYWORDS?|Highlights|HIGHLIGHTS)\b")
BLOCK_MAX = 850  # 保留餘裕：區塊超過 Dify 父區塊上限 1024 時，Dify 會在換行處再切，標題會被切成獨立小塊
BLOCK_HARD_MAX = 1000  # 絕對上限，仍低於 Dify 的 1024


def drop_front_matter(paras):
    """刪掉摘要之前的作者、單位、Keywords（它們會變成關鍵字密集的小區塊，搶走檢索排名）。"""
    for i, p in enumerate(paras[:40]):
        if START_RE.match(p):
            paras = paras[i:]
            break
    return [p for p in paras if not KEYWORDS_RE.match(p)]


def split_long(p, limit=BLOCK_MAX):
    """超過上限的段落在句子邊界切成長度相近的幾份（避免尾巴剩一句變成小區塊）。
    單句本身就超過上限時，再退一步在分號／逗號處切。"""
    if len(p) <= limit:
        return [p]
    units = []
    for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", p):
        units += [s] if len(s) <= limit else re.split(r"(?<=[;,])\s+", s)
    target = len(p) / math.ceil(len(p) / limit)
    parts, cur = [], ""
    for s in units:
        if cur and (len(cur) + len(s) + 1 > limit or len(cur) >= target):
            parts.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        parts.append(cur)
    return parts


def group_blocks(paras, header):
    paras = [piece for p in paras for piece in (split_long(p) if not p.startswith("## ") else [p])]
    """把短段落併成約 1000 字的區塊；區塊之間空一行（父區塊分隔），區塊內用換行（子區塊分隔）。
    章節標題一律開新區塊，並跟著下一段，不會自己變成一個區塊。"""
    blocks, cur = [], [header]
    size = len(header)
    for p in paras:
        if p.startswith("## "):
            if cur and not all(c.startswith(("## ", "# ", "DOI:")) for c in cur):
                blocks.append("\n".join(cur))
                cur, size = [], 0
            cur.append(p)
            size += len(p)
            continue
        only_heading = all(c.startswith(("## ", "# ", "DOI:")) for c in cur)
        # 超過 850 就換區塊；但目前區塊還很小（<300）時允許放到 1000，避免留下一兩句的孤兒區塊
        if cur and not only_heading and size + len(p) > (BLOCK_MAX if size >= 300 else BLOCK_HARD_MAX):
            blocks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(p)
        size += len(p)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def main(src: str, dst: str):
    src_dir, dst_dir = pathlib.Path(src), pathlib.Path(dst)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dois = []
    for pdf in sorted(src_dir.glob("*.pdf")):
        raw = pdf_to_text(pdf)
        m = DOI_RE.search(raw)
        doi = m.group(1) if m else ""
        title = re.sub(r"\s+", " ", pdf.stem).strip()
        paras = drop_front_matter(clean(raw))
        header = f"# {title}\nDOI: https://doi.org/{doi}" if doi else f"# {title}\nDOI: (not found)"
        body = "\n\n".join(group_blocks(paras, header)) + "\n"
        # Dify 解析 .md 時會在每個 # 標題處強制切段，標題+DOI 會變成獨立小區塊；
        # 所以輸出純文字 .txt，並拿掉 # 記號（章節標題仍保留在區塊第一行）。
        body = re.sub(r"(?m)^#{1,2} ", "", body).replace(f"{title}\nDOI:", f"Title: {title}\nDOI:", 1)
        (dst_dir / f"{title}.txt").write_text(body, encoding="utf-8")
        dois.append(doi or f"# DOI not found: {title}")
        print(f"{len(raw)//1000:>4}k -> {len(body)//1000:>4}k  {doi:<45} {title[:60]}")
    (dst_dir.parent / "dois.txt").write_text("\n".join(dois) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
