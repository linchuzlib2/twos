# -*- coding: utf-8 -*-
"""附件文本提取：用于全文搜索附件内容
支持：txt/md/csv/json/log/html/xml 等纯文本、docx、xlsx、pptx（Office Open XML）、pdf
旧版二进制 .doc/.xls/.ppt 不支持（返回空）。
"""
import io
import logging
import re
import zipfile

log = logging.getLogger("extract")

MAX_TEXT = 200_000  # 每个附件最多提取 20 万字符
INDEX_VERSION = 2

PLAIN_EXTS = (
    ".txt", ".md", ".mdx", ".rst", ".csv", ".tsv", ".json", ".ipynb",
    ".log", ".html", ".htm", ".xml", ".xhtml", ".css", ".scss", ".less",
    ".js", ".jsx", ".ts", ".tsx", ".vue", ".py", ".rb", ".php", ".java",
    ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs",
    ".r", ".sh", ".bash", ".ps1", ".bat", ".sql", ".ini", ".cfg",
    ".conf", ".properties", ".toml", ".yml", ".yaml", ".srt", ".tex",
)

OOXML_PARTS = {
    ".docx": ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"),
    ".xlsx": ("xl/sharedStrings.xml",),
    ".pptx": None,  # 动态匹配 ppt/slides/slideN.xml
}

SEARCHABLE_EXTS = PLAIN_EXTS + (".docx", ".xlsx", ".pptx", ".pdf")


def supports_extraction(filename: str) -> bool:
    return (filename or "").lower().endswith(SEARCHABLE_EXTS)


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _clean_xml_text(xml: str) -> str:
    xml = re.sub(r"</w:p>|</a:p>|</t>", "\n", xml)
    xml = re.sub(r"<[^>]+>", " ", xml)
    return re.sub(r"[ \t]+", " ", xml)


def _extract_ooxml(data: bytes, ext: str) -> str:
    parts = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if ext == ".pptx":
            targets = [n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        elif ext == ".xlsx":
            targets = [n for n in names if (
                n == "xl/sharedStrings.xml"
                or re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
            )]
        else:
            targets = [n for n in OOXML_PARTS[ext] if n in names]
        for n in targets:
            try:
                parts.append(_clean_xml_text(z.read(n).decode("utf-8", "ignore")))
            except Exception:
                continue
    return "\n".join(p for p in parts if p.strip())


def extract_text(data: bytes, filename: str) -> str:
    """从支持的附件中提取文本；格式损坏时记录并抛出异常，避免标记为已索引。"""
    if not data:
        return ""
    name = (filename or "").lower()
    try:
        if name.endswith(PLAIN_EXTS):
            return _decode_text(data)[:MAX_TEXT]
        if name.endswith((".docx", ".xlsx", ".pptx")):
            ext = next((e for e in OOXML_PARTS if name.endswith(e)), None)
            if ext:
                return _extract_ooxml(data, ext)[:MAX_TEXT]
        if name.endswith(".pdf"):
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise RuntimeError("PDF 文本提取依赖 pypdf 未安装") from exc
            reader = PdfReader(io.BytesIO(data))
            pages = []
            for p in reader.pages[:200]:
                try:
                    pages.append(p.extract_text() or "")
                except Exception:
                    continue
            return "\n".join(pages)[:MAX_TEXT]
    except Exception:
        log.exception("附件文本提取失败: %s", filename)
        raise
    return ""
