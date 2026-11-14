"""Build small but realistic .docx files for tests (no Word needed).

Paragraph spec:
  "plain text"                       one run ("\\n" -> w:br, "\\t" -> w:tab)
  [run, run, ...]                    several runs; run = "text" | ("text", {"b": True, ...})
                                     | {"footnote": 1} | {"field": "PAGE", "result": "1"}
                                     | {"textbox": [para, ...]} | {"hyperlink": "mailto:x", "text": "x"}
  {"table": [[cell, cell], ...]}     cell = paragraph spec or {"paras": [paragraph spec, ...]}
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = (
    f'xmlns:w="{W}" xmlns:r="{R}" xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" mc:Ignorable="w14"'
)


class _Ctx:
    def __init__(self) -> None:
        self.hyperlinks: list[tuple[str, str]] = []


def _rpr(props: dict) -> str:
    parts = []
    if props.get("b"):
        parts.append("<w:b/>")
    if props.get("i"):
        parts.append("<w:i/>")
    if props.get("color"):
        parts.append(f'<w:color w:val="{props["color"]}"/>')
    if props.get("sz"):
        parts.append(f'<w:sz w:val="{props["sz"]}"/>')
    if props.get("highlight"):
        parts.append(f'<w:highlight w:val="{props["highlight"]}"/>')
    if props.get("u"):
        parts.append('<w:u w:val="single"/>')
    return f"<w:rPr>{''.join(parts)}</w:rPr>" if parts else ""


def _text_content(text: str) -> str:
    out = []
    buf = ""
    for ch in text:
        if ch in "\n\t":
            if buf:
                out.append(f'<w:t xml:space="preserve">{escape(buf)}</w:t>')
                buf = ""
            out.append("<w:br/>" if ch == "\n" else "<w:tab/>")
        else:
            buf += ch
    if buf:
        out.append(f'<w:t xml:space="preserve">{escape(buf)}</w:t>')
    return "".join(out)


def _run(spec, ctx: _Ctx) -> str:
    if isinstance(spec, str):
        return f'<w:r w:rsidR="00A1B2C3">{_text_content(spec)}</w:r>'
    if isinstance(spec, tuple):
        text, props = spec
        return f'<w:r w:rsidR="00A1B2C3">{_rpr(props)}{_text_content(text)}</w:r>'
    if "footnote" in spec:
        return (f'<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
                f'<w:footnoteReference w:id="{spec["footnote"]}"/></w:r>')
    if "field" in spec:
        return (f'<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                f'<w:r><w:instrText xml:space="preserve"> {escape(spec["field"])} </w:instrText></w:r>'
                f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
                f'<w:r>{_text_content(spec.get("result", ""))}</w:r>'
                f'<w:r><w:fldChar w:fldCharType="end"/></w:r>')
    if "textbox" in spec:
        inner = "".join(_para(p, ctx) for p in spec["textbox"])
        return ('<w:r><w:pict><v:shape id="tb1" style="width:200pt;height:60pt">'
                f'<v:textbox><w:txbxContent>{inner}</w:txbxContent></v:textbox></v:shape></w:pict></w:r>')
    if "hyperlink" in spec:
        rid = f"rIdH{len(ctx.hyperlinks) + 1}"
        ctx.hyperlinks.append((rid, spec["hyperlink"]))
        return f'<w:hyperlink r:id="{rid}"><w:r>{_text_content(spec["text"])}</w:r></w:hyperlink>'
    raise ValueError(spec)


def _para(spec, ctx: _Ctx) -> str:
    if isinstance(spec, dict) and "table" in spec:
        rows = []
        for row in spec["table"]:
            cells = []
            for cell in row:
                paras = cell["paras"] if isinstance(cell, dict) and "paras" in cell else [cell]
                body = "".join(_para(p, ctx) for p in paras) or "<w:p/>"
                cells.append(f'<w:tc><w:tcPr><w:tcW w:w="4000" w:type="dxa"/></w:tcPr>{body}</w:tc>')
            rows.append(f"<w:tr>{''.join(cells)}</w:tr>")
        grid = "".join('<w:gridCol w:w="4000"/>' for _ in spec["table"][0])
        return (f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>'
                f"{''.join(rows)}</w:tbl>")
    runs = [spec] if isinstance(spec, str) else spec
    return f'<w:p w:rsidR="00A1B2C3" w:rsidRDefault="00A1B2C3">{"".join(_run(r, ctx) for r in runs)}</w:p>'


def _rels(items: list[tuple[str, str, str, bool]]) -> str:
    body = "".join(
        f'<Relationship Id="{i}" Type="{t}" Target="{escape(tg)}"' + (' TargetMode="External"' if ext else "") + "/>"
        for i, t, tg, ext in items
    )
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{body}</Relationships>')


REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
CT_W = "application/vnd.openxmlformats-officedocument.wordprocessingml."


def make_docx(
    path: str | Path | None = None,
    body: list | None = None,
    header: list | None = None,
    footer: list | None = None,
    footnotes: dict[int, list] | None = None,
    core: dict[str, str] | None = None,
    company: str | None = None,
    custom: dict[str, str] | None = None,
    people: list[str] | None = None,
) -> bytes:
    ctx = _Ctx()
    files: dict[str, str] = {}
    overrides = [("/word/document.xml", CT_W + "document.main+xml"),
                 ("/word/settings.xml", CT_W + "settings+xml"),
                 ("/docProps/core.xml", "application/vnd.openxmlformats-package.core-properties+xml"),
                 ("/docProps/app.xml", "application/vnd.openxmlformats-officedocument.extended-properties+xml")]
    doc_rels: list[tuple[str, str, str, bool]] = [("rIdS", REL + "settings", "settings.xml", False)]
    sect_refs = ""
    if header:
        files["word/header1.xml"] = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:hdr {NS}>'
                                     + "".join(_para(p, ctx) for p in header) + "</w:hdr>")
        overrides.append(("/word/header1.xml", CT_W + "header+xml"))
        doc_rels.append(("rIdH", REL + "header", "header1.xml", False))
        sect_refs += '<w:headerReference w:type="default" r:id="rIdH"/>'
    if footer:
        files["word/footer1.xml"] = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:ftr {NS}>'
                                     + "".join(_para(p, ctx) for p in footer) + "</w:ftr>")
        overrides.append(("/word/footer1.xml", CT_W + "footer+xml"))
        doc_rels.append(("rIdF", REL + "footer", "footer1.xml", False))
        sect_refs += '<w:footerReference w:type="default" r:id="rIdF"/>'
    if footnotes:
        notes = ('<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
                 '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>')
        for fid, paras in footnotes.items():
            inner = ""
            for i, p in enumerate(paras):
                runs = [p] if isinstance(p, str) else list(p)
                pre = ('<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:footnoteRef/></w:r>'
                       '<w:r><w:t xml:space="preserve"> </w:t></w:r>') if i == 0 else ""
                inner += f"<w:p>{pre}{''.join(_run(r, ctx) for r in runs)}</w:p>"
            notes += f'<w:footnote w:id="{fid}">{inner}</w:footnote>'
        files["word/footnotes.xml"] = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:footnotes {NS}>'
                                       f"{notes}</w:footnotes>")
        overrides.append(("/word/footnotes.xml", CT_W + "footnotes+xml"))
        doc_rels.append(("rIdN", REL + "footnotes", "footnotes.xml", False))
    body_xml = "".join(_para(p, ctx) for p in (body or []))
    files["word/document.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {NS}><w:body>{body_xml}'
        f'<w:sectPr w:rsidR="00A1B2C3">{sect_refs}<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1417" w:right="1417" w:bottom="1134" w:left="1417" w:header="708" w:footer="708" w:gutter="0"/>'
        "</w:sectPr></w:body></w:document>"
    )
    for rid, target in ctx.hyperlinks:
        doc_rels.append((rid, REL + "hyperlink", target, True))
    files["word/settings.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings {NS}><w:zoom w:percent="100"/>'
        '<w:docVars><w:docVar w:name="Bearbeiter" w:val="Hans Muster"/></w:docVars>'
        '<w:rsids><w:rsidRoot w:val="00A1B2C3"/><w:rsid w:val="00A1B2C3"/></w:rsids></w:settings>'
    )
    core = core or {"creator": "Hans Muster", "lastModifiedBy": "Erika Beispiel", "title": "Urteil Kaiser"}
    core_xml = "".join(
        f"<dc:{k}>{escape(v)}</dc:{k}>" if k in ("creator", "title", "subject", "description")
        else f"<cp:{k}>{escape(v)}</cp:{k}>"
        for k, v in core.items()
    )
    files["docProps/core.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">{core_xml}</cp:coreProperties>'
    )
    files["docProps/app.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties '
        'xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        f"<Application>Microsoft Office Word</Application><Company>{escape(company or 'Fürstlicher Gerichtshof')}</Company>"
        "<Manager>Hans Muster</Manager></Properties>"
    )
    pkg_rels = [("rId1", REL + "officeDocument", "word/document.xml", False),
                ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "docProps/core.xml", False),
                ("rId3", REL + "extended-properties", "docProps/app.xml", False)]
    if custom:
        props = "".join(
            f'<property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="{i + 2}" name="{escape(k)}">'
            f'<vt:lpwstr>{escape(v)}</vt:lpwstr></property>'
            for i, (k, v) in enumerate(custom.items())
        )
        files["docProps/custom.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties '
            'xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
            f'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">{props}</Properties>'
        )
        overrides.append(("/docProps/custom.xml", "application/vnd.openxmlformats-officedocument.custom-properties+xml"))
        pkg_rels.append(("rId4", REL + "custom-properties", "docProps/custom.xml", False))
    if people:
        ppl = "".join(f'<w15:person w15:author="{escape(p)}"/>' for p in people)
        files["word/people.xml"] = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                                    '<w15:people xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">'
                                    f"{ppl}</w15:people>")
        overrides.append(("/word/people.xml", CT_W + "people+xml"))
        doc_rels.append(("rIdP", "http://schemas.microsoft.com/office/2011/relationships/people", "people.xml", False))
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          + "".join(f'<Override PartName="{p}" ContentType="{c}"/>' for p, c in overrides) + "</Types>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr("_rels/.rels", _rels(pkg_rels))
        zf.writestr("word/_rels/document.xml.rels", _rels(doc_rels))
        for name, content in files.items():
            zf.writestr(name, content)
    data = buf.getvalue()
    if path is not None:
        Path(path).write_bytes(data)
    return data
