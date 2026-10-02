# -*- coding: utf-8 -*-
"""附件文本提取：用于全文搜索附件内容
支持：txt/md/csv/json/log/html/xml 等纯文本、docx、xlsx、pptx（Office Open XML）、pdf
旧版二进制 .doc/.xls/.ppt 不支持（返回空）。
"""
import io
import re
import zipfile

MAX_TEXT = 200_000  # 每个附件最多提取 20 万字符

PLAIN_EXTS = (
    ".txt", ".md", ".csv", ".json", ".log", ".html", ".htm", ".xml",
    ".py", ".js", ".css", ".ini", ".yml", ".yaml", ".sql", ".bat", ".srt",
)

OOXML_PARTS = {
    ".docx": ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"),
    ".xlsx": ("xl/sharedStrings.xml",),
    ".pptx": None,  # 动态匹配 ppt/slides/slideN.xml
}


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
        else:
            targets = [n for n in OOXML_PARTS[ext] if n in names]
        for n in targets:
            try:
                parts.append(_clean_xml_text(z.read(n).decode("utf-8", "ignore")))
            except Exception:
                continue
    return "\n".join(p for p in parts if p.strip())


def extract_text(data: bytes, filename: str) -> str:
    """从附件字节中提取纯文本（失败返回空字符串）"""
    if not data:
        return ""
    name = (filename or "").lower()
    try:
        if name.endswith(PLAIN_EXTS):
            return data.decode("utf-8", "ignore")[:MAX_TEXT]
        if name.endswith((".docx", ".xlsx", ".pptx")):
            ext = next((e for e in OOXML_PARTS if name.endswith(e)), None)
            if ext:
                return _extract_ooxml(data, ext)[:MAX_TEXT]
        if name.endswith(".pdf"):
            try:
                from pypdf import PdfReader
            except ImportError:
                return ""
            reader = PdfReader(io.BytesIO(data))
            pages = []
            for p in reader.pages[:200]:
                try:
                    pages.append(p.extract_text() or "")
                except Exception:
                    continue
            return "\n".join(pages)[:MAX_TEXT]
    except Exception:
        return ""
    return ""
