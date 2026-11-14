# -*- coding: utf-8 -*-
"""从消息文案里抽标题/年份，并生成安全的文件名。

规则（可用 --name-mode 选择用不用）：
  * 文案里有《书名号》就取书名号内的文字当标题
  * 没有书名号就取第一行有效文本（去掉表情与多余空白）
  * 文案里的 (2019) / （2019） 当作年份
"""
import re

YEAR_RE = re.compile(r"[（(]\s*(\d{4})\s*[）)]")
TITLE_RE = re.compile(r"《([^》]{1,160})》")
ILLEGAL = str.maketrans({
    "<": " ", ">": " ", ":": " ", '"': " ", "/": " ", "\\": " ",
    "|": " ", "?": " ", "*": " ", "\n": " ", "\r": " ", "\t": " ",
})


def _clean(s):
    if not s:
        return ""
    s = s.replace("\u200b", "").replace("\xa0", " ")
    s = s.translate(ILLEGAL)
    s = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", s)  # 去掉表情
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -_·.,，。:：")


def parse_caption(text):
    """返回 {"title": str, "year": str, "headline": str}。"""
    out = {"title": "", "year": "", "headline": ""}
    if not text:
        return out
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out["headline"] = _clean(lines[0]) if lines else ""
    m = TITLE_RE.search(text)
    if m:
        out["title"] = _clean(m.group(1))
    elif out["headline"]:
        out["title"] = out["headline"]
    y = YEAR_RE.search(text)
    if y:
        out["year"] = y.group(1)
    return out


def build_name(title, year):
    """生成 《标题》（年份） 形式的基名；没有标题返回 None。"""
    if not title:
        return None
    name = "《%s》" % title
    if year:
        name += "（%s）" % year
    return name


def sanitize(name, maxlen=150):
    name = _clean(name)
    if len(name) > maxlen:
        name = name[:maxlen].rstrip()
    return name
