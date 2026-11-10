"""把微信合并转发的内嵌文字整理成一条有原作者标注的聊天资料。"""
import xml.etree.ElementTree as ET


def forwarded_text(source):
    start = source.find("<")
    if start < 0 or "<recorditem" not in source or "<!DOCTYPE" in source or "<!ENTITY" in source:
        return ""
    try:
        root = ET.fromstring(source[start:])
        app = root if root.tag == "appmsg" else root.find("appmsg")
        if app is None or (app.findtext("type") or "").strip() != "19":
            return ""
        inner = app.findtext("recorditem") or ""
        if not inner.strip() or "<!DOCTYPE" in inner or "<!ENTITY" in inner:
            return ""
        records = ET.fromstring(inner).findall("./datalist/dataitem")
    except ET.ParseError:
        return ""
    title = " ".join((app.findtext("title") or "聊天记录").split())[:100]
    lines = []
    for record in records:
        if record.get("datatype") != "1":
            continue
        text = " ".join((record.findtext("datadesc") or "").split())
        if text:
            name = " ".join((record.findtext("sourcename") or "原发言人未知").split())[:80]
            stamp = " ".join((record.findtext("sourcetime") or "").split())[:40]
            lines.append(f"{stamp + ' ' if stamp else ''}{name}：{text}")
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > 12000:
        body = body[:8000] + "\n（中间部分省略）\n" + body[-4000:]
    return f"[合并转发] {title}\n{body}"
