"""Art's «Шестерня узла КК» (helical gear m 0.5, z 20, β 15°; SOLIDWORKS/KOMPAS PDF, title block 5:1).
What must hold: the scale comes from the dimensions (the PDF page is printed √2 larger than its
A3 format, so the title block's 5:1 gives every size 1.4× too big), «11,46*» is read, the gear's
face view — not the bigger isometric picture — is the main view, and the overall size is
Ø11.46 × 6. The comparison of the program's reading with a language model's is checked offline
on a made-up answer; with --llm the local model really reads the sheet (LM Studio, minutes)."""
import asyncio
import os
import shutil
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import app  # noqa: E402
import drawing  # noqa: E402

uid = uuid.uuid4().hex
folder = os.path.join(app.UPLOADS, uid)
os.makedirs(folder)
shutil.copy(os.path.join(HERE, "pdfvec", "gear.pdf"), os.path.join(folder, "source.pdf"))
assert app._vector_pdf(os.path.join(folder, "source.pdf"), folder)
vec = app._vector_meta(os.path.join(folder, "source_vector.png"))
assert 11.46 in vec["numbers"]["values"], vec["numbers"]
lay = app._vector_layout(vec)
assert lay and lay["side"] is not None and lay["top"] is None, lay
views = [{"url": f"/uploads/{uid}/{v['file']}", "box": v["box"]} for v in vec["views"]]
reading = app._vector_reading(vec, [app._upload_path(v["url"]) for v in views], [v["box"] for v in views])
sizes = sorted(d["value"] for d in reading["dimensions"] if d["overall"])
assert sizes == [6.0, 11.46, 11.46], sizes
a = {"id": "t", "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
     "values": {d["id"]: d["value"] for d in reading["dimensions"]}, "vector": vec}
app._geometry(a)
notes = a["geometry"]["notes"]
print("\n".join(notes))
# the teeth counted on the main outline, the helix read off the tooth lines on the view beside
assert any(n.startswith("Косозубое колесо: 20 зубьев") and "правое" in n for n in notes), notes
assert any(n.startswith("Ступица Ø7 высотой 2.5") for n in notes), notes   # a hub, not a hole
assert any(n.startswith("1 отв. Ø4 насквозь") for n in notes), notes
assert any("глубина 2.2" in n for n in notes), notes                         # the slot's floor
ops = a["reading"]["plan"]["operations"]
# the root cylinder, one tooth through sections along the helix (a short contour KOMPAS pairs
# reliably), the tooth copied round
assert [o["op"] for o in ops[:3]] == ["extrude", "loft", "circular"], [o["op"] for o in ops[:3]]
assert len(ops[1]["sections"]) >= 5 and len(ops[1]["sections"][0]["shapes"]) == 6 and ops[2]["count"] == 20
if "--build" in sys.argv:
    import json
    import urllib.request
    import kcheck
    body = json.dumps({"name": "build_part", "args": {"operations": ops, "name": "gear", "screenshot": False, "close": True,
                                                      "keep_going": True}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/tool", body,
                                                                {"Content-Type": "application/json"}), timeout=900))
    res = json.loads(r["text"])
    vol = res.get("volume_mm3") or 0
    print("built", res.get("operations_done"), "/", len(ops), "volume", vol, "size", res.get("size_mm"))
    # rim 3.3 + hub Ø7×2.5 + boss Ø9×0.2 − bore Ø4 − slot: about 355 mm³ (the boss's 0.2 band beyond Ø9
    # stays, a few mm³)
    assert 285 <= vol <= 305 and kcheck.check(res) and not res.get("failed"), res
# the comparison: a model that sees Ø4 the program did not use and a different overall length
numbers = app._vector_numbers(a)
llm = {"dimensions": [{"id": "d1", "value": 11.46, "axis": "height"}, {"id": "d2", "value": 6, "axis": "width"},
                      {"id": "d3", "value": 4, "axis": "diameter"}, {"id": "d4", "value": 30, "axis": "length"}],
       "overall": {"length": "d4", "width": "d2", "height": "d1"},
       "review": ["Программа пропустила отверстие Ø4"]}
issues = drawing.compare_readings(numbers, reading, a["values"], llm)
texts = " | ".join(i["text"] for i in issues)
assert "длина X: программа 11.46 мм, модель 30 мм" in texts, texts
assert "Модель ИИ: Программа пропустила отверстие Ø4" in texts, texts
assert "30" in next(i["text"] for i in issues if "которых нет у программы" in i["text"]), texts
if "--llm" in sys.argv:
    async def run():
        async for _ in app._crosscheck(a, os.path.join(folder, "source_vector.png"), "косозубая шестерня",
                                       app._drawing_settings(), set()):
            pass
    asyncio.run(run())
    cc = a["crosscheck"]
    print("model reading:", cc.get("features"))
    for i in cc["issues"]:
        print("  ", i["level"], i["text"])
    assert not cc.get("error"), cc.get("error")
print("PDF GEAR TEST PASSED")
shutil.rmtree(folder, ignore_errors=True)
