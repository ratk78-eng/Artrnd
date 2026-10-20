"""Uploads a PDF/image to the running UI (http://127.0.0.1:8765) and prints the rendered page URLs."""
import json
import os
import sys
import urllib.request
import uuid

path = sys.argv[1]
url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8765"
boundary = uuid.uuid4().hex
with open(path, "rb") as f:
    data = f.read()
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{os.path.basename(path)}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
req = urllib.request.Request(url + "/api/upload", data=body,
                             headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
with urllib.request.urlopen(req, timeout=300) as r:
    res = json.loads(r.read())
print(json.dumps(res, ensure_ascii=False))
assert res.get("pages"), res
for p in res["pages"]:
    with urllib.request.urlopen(url + p) as r:
        assert r.read(8).startswith(b"\x89PNG"), p
print("UPLOAD OK", len(res["pages"]), "pages")
