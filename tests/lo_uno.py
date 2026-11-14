"""LibreOffice helper, run with the *system* python that has `uno`.

usage: python3 lo_uno.py OUT.json FILE.docx [FILE.docx ...]
       python3 lo_uno.py --simulate REVIEW.docx SAVED.docx REJECT_AUTHOR

For every file: load it in a headless LibreOffice, record the redlines
(author, type) and the plain text in three views: as loaded, after
"Accept All" and after "Reject All". Writes a JSON dict keyed by file path.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

import uno  # noqa: F401  (system python3-uno)
from com.sun.star.beans import PropertyValue


def pv(name, value):
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def connect():
    pipe = f"anonpipe{os.getpid()}"
    profile = tempfile.mkdtemp(prefix="lo_profile_")
    proc = subprocess.Popen(
        ["soffice", "--headless", "--invisible", "--norestore", "--nologo", "--nodefault",
         f"--accept=pipe,name={pipe};urp;", f"-env:UserInstallation=file://{profile}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
    for _ in range(120):
        try:
            ctx = resolver.resolve(f"uno:pipe,name={pipe};urp;StarOffice.ComponentContext")
            return proc, ctx
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("LibreOffice did not start")


def text_of(doc, tmpdir, tag):
    out = os.path.join(tmpdir, f"{tag}.txt")
    doc.storeToURL(uno.systemPathToFileUrl(out), (pv("FilterName", "Text (encoded)"), pv("FilterOptions", "UTF8,LF,,")))
    with open(out, encoding="utf-8-sig") as fh:
        return fh.read()


def redlines(doc):
    out = []
    enum = doc.Redlines.createEnumeration()
    while enum.hasMoreElements():
        r = enum.nextElement()
        out.append([r.getPropertyValue("RedlineAuthor"), r.getPropertyValue("RedlineType")])
    return out


def simulate_reviewer(src, dst, reject_author):
    """Reject every change by `reject_author`, accept the rest, delete all
    comments, save as .docx (what a reviewer does in LibreOffice)."""
    proc, ctx = connect()
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    try:
        doc = desktop.loadComponentFromURL(uno.systemPathToFileUrl(os.path.abspath(src)), "_blank", 0,
                                           (pv("Hidden", True),))
        frame = doc.CurrentController.Frame
        for _ in range(1000):
            target = None
            e = doc.Redlines.createEnumeration()
            while e.hasMoreElements():
                r = e.nextElement()
                if r.getPropertyValue("RedlineAuthor") == reject_author:
                    target = r
                    break
            if target is None:
                break
            start, end = target.getPropertyValue("RedlineStart"), target.getPropertyValue("RedlineEnd")
            cur = start.getText().createTextCursorByRange(start)
            cur.gotoRange(end, True)
            doc.CurrentController.select(cur)
            dispatcher.executeDispatch(frame, ".uno:RejectTrackedChange", "", 0, ())
        dispatcher.executeDispatch(frame, ".uno:AcceptAllTrackedChanges", "", 0, ())
        anns = []
        fields = doc.TextFields.createEnumeration()
        while fields.hasMoreElements():
            f = fields.nextElement()
            if f.supportsService("com.sun.star.text.TextField.Annotation"):
                anns.append(f)
        for f in anns:
            f.dispose()
        doc.storeToURL(uno.systemPathToFileUrl(os.path.abspath(dst)), (pv("FilterName", "MS Word 2007 XML"),))
        doc.close(True)
    finally:
        try:
            desktop.terminate()
        except Exception:
            pass
        proc.wait(timeout=30)


def main():
    if sys.argv[1] == "--simulate":
        simulate_reviewer(sys.argv[2], sys.argv[3], sys.argv[4])
        return
    out_json = sys.argv[1]
    files = sys.argv[2:]
    proc, ctx = connect()
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    result = {}
    tmpdir = tempfile.mkdtemp(prefix="lo_txt_")
    try:
        for i, path in enumerate(files):
            entry = {}
            url = uno.systemPathToFileUrl(os.path.abspath(path))
            for view in ("loaded", "accept", "reject"):
                doc = desktop.loadComponentFromURL(url, "_blank", 0, (pv("Hidden", True),))
                if doc is None:
                    entry["error"] = "load failed"
                    break
                if view == "loaded":
                    entry["redlines"] = redlines(doc)
                    comments = 0
                    fields = doc.TextFields.createEnumeration()
                    while fields.hasMoreElements():
                        f = fields.nextElement()
                        if f.supportsService("com.sun.star.text.TextField.Annotation"):
                            comments += 1
                    entry["comments"] = comments
                else:
                    cmd = ".uno:AcceptAllTrackedChanges" if view == "accept" else ".uno:RejectAllTrackedChanges"
                    dispatcher.executeDispatch(doc.CurrentController.Frame, cmd, "", 0, ())
                entry[view] = text_of(doc, tmpdir, f"{i}_{view}")
                doc.close(True)
            result[path] = entry
    finally:
        try:
            desktop.terminate()
        except Exception:
            pass
        proc.wait(timeout=30)
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False)


if __name__ == "__main__":
    main()
