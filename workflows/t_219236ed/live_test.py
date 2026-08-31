#!/usr/bin/env python3
"""Live verification of second-iteration interface_RAG (decisions 20-29)."""
import json, os, stat, urllib.request, urllib.error

BASE = "http://127.0.0.1:8094"
UPLOAD_DIR = "/root/projects/interface_RAG/uploads"
MD_DIR = "/root/projects/interface_RAG/uploads/Markdown"
ENV_FILE = "/root/projects/interface_RAG/config/.env"

def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

def upload(path_bytes, filename):
    import uuid
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + path_bytes + \
           f"\r\n--{boundary}--\r\n".encode()
    r = urllib.request.Request(BASE + "/api/documents", data=body, method="POST",
                               headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read().decode())

def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)

MIN_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
           b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
           b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
           b"4 0 obj<</Length 44>>stream\nBT /F1 24 Tf 72 720 Td (GOST TEST DOC) Tj ET\nendstream endobj\n"
           b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
           b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000254 00000 n \n0000000346 00000 n \ntrailer<</Size 6/Root 1 0 R>>\nstartxref\n409\n%%EOF\n")

print("=" * 70)
print("TEST 1: upload -> file perms 0666, dir 0777")
res = upload(MIN_PDF, "GOST_9999_test.pdf")
sid = res["session_id"]; stem = res["stem"]
print("  upload resp:", res)
up = os.path.join(UPLOAD_DIR, "GOST_9999_test.pdf")
print("  uploaded file mode: %o (expect 666)" % mode(up))
print("  uploads dir mode:   %o (expect 777)" % mode(UPLOAD_DIR))

print("=" * 70)
print("TEST 2: register -> _reg.yaml perms, slug")
fields = {
    "document_id": "ГОСТ 9999-99", "document_type": "ГОСТ", "title": "Тестовый кабель",
    "domain": "Кабели", "edition": 1999, "date_enacted": "1999-01-01",
    "status": "active", "ignore_sections": ["Предисловие", "Содержание"],
}
st, r = req("POST", f"/api/documents/{sid}/register", {"fields": fields})
print("  register:", st, r)
slug1 = r.get("slug")
reg = os.path.join(MD_DIR, stem, f"{stem}_reg.yaml")
print("  reg dir mode:  %o (expect 777)" % mode(os.path.dirname(reg)))
print("  _reg.yaml mode: %o (expect 666)" % mode(reg))

print("=" * 70)
print("TEST 3: prefill from existing _reg.yaml (no OCR)")
st, r = req("POST", f"/api/documents/{sid}/register/prefill")
print("  source:", r.get("source"), "exists:", r.get("exists"))
print("  fields keys:", sorted(r.get("fields", {}).keys()))
print("  title:", r.get("fields", {}).get("title"), "| status:", r.get("fields", {}).get("status"),
      "| ignore_sections:", r.get("fields", {}).get("ignore_sections"))
print("  slug in fields:", r.get("fields", {}).get("slug"))

print("=" * 70)
print("TEST 4: re-register (changed title) -> one record, SAME slug")
fields["title"] = "Тестовый кабель ИЗМЕНЁН"
st, r = req("POST", f"/api/documents/{sid}/register", {"fields": fields})
slug2 = r.get("slug")
print("  slug1=%s slug2=%s  SAME=%s" % (slug1, slug2, slug1 == slug2))
import yaml
doc = yaml.safe_load(open(reg, encoding="utf-8"))
docs = doc.get("documents", {})
print("  documents count in _reg.yaml:", len(docs), "(expect 1)")
print("  title after re-register:", list(docs.values())[0].get("title"))
print("  .bak mode: %o (expect 666)" % mode(reg + ".bak"))

print("=" * 70)
print("TEST 5: fresh upload same file -> prefill source=reg_yaml (sid2)")
res2 = upload(MIN_PDF, "GOST_9999_test.pdf")
sid2 = res2["session_id"]
st, r = req("POST", f"/api/documents/{sid2}/register/prefill")
print("  sid2 source:", r.get("source"), "(expect reg_yaml)", "exists:", r.get("exists"))

print("=" * 70)
print("TEST 6: qdrant folder API")
st, r = req("GET", "/api/settings/qdrant")
print("  initial:", r)
st, r = req("PUT", "/api/settings/qdrant", {"path": "/tmp/review_test_qdrant"})
print("  PUT valid:", st, r, "(expect overridden:true, path=/tmp/review_test_qdrant)")
print("  .env mode after write: %o (expect 600)" % mode(ENV_FILE))
st, r = req("PUT", "/api/settings/qdrant", {"path": "relative/path"})
print("  PUT relative:", st, r, "(expect 400)")
st, r = req("PUT", "/api/settings/qdrant", {"path": "../escape"})
print("  PUT traversal:", st, r, "(expect 400)")
st, r = req("PUT", "/api/settings/qdrant", {"path": ""})
print("  PUT reset:", st, r, "(expect overridden:false)")

print("=" * 70)
print("TEST 7: .env contents after reset (QDRANT_PATH removed)")
with open(ENV_FILE, encoding="utf-8") as f:
    has_qdrant = any("QDRANT_PATH" in line for line in f)
print("  QDRANT_PATH in .env:", has_qdrant, "(expect False)")
print("  .env mode: %o (expect 600)" % mode(ENV_FILE))

print("=" * 70)
print("ALL LIVE TESTS DONE")
