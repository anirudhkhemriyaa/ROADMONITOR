#!/usr/bin/env python3
"""Upload a finished drive to the backend (stdlib only).
Usage: python ~/rcm/upload.py D001            (server address read from ~/rcm/server.txt)
       python ~/rcm/upload.py D001 http://192.168.1.5:8000"""
import glob, json, os, sys, urllib.error, urllib.request, uuid

D = os.path.expanduser("~/rcm")
if len(sys.argv) < 2:
    sys.exit(__doc__)
drive = sys.argv[1]
base = sys.argv[2] if len(sys.argv) > 2 else open(os.path.join(D, "server.txt")).read().strip()
csvs = [p for p in glob.glob(os.path.join(D, "drives", drive + "_*.csv")) if not p.endswith("_markers.csv")]
if not csvs:
    sys.exit("No CSV found for " + drive)
meta_p = os.path.join(D, "drives", drive + "_meta.json")
meta = json.load(open(meta_p)) if os.path.exists(meta_p) else {}

b = uuid.uuid4().hex
body = b""
def field(name, value):
    return ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, name, value)).encode()
def file_part(name, path):
    head = ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            'Content-Type: text/csv\r\n\r\n' % (b, name, os.path.basename(path))).encode()
    return head + open(path, "rb").read() + b"\r\n"

body += field("vehicle_type", meta.get("vehicle_type", "unknown"))
body += field("phone_model", meta.get("phone_model", ""))
body += field("meta", json.dumps(meta))
body += file_part("file", csvs[0])
mk = os.path.join(D, "drives", drive + "_markers.csv")
if os.path.exists(mk):
    body += file_part("markers", mk)
body += ("--%s--\r\n" % b).encode()

req = urllib.request.Request(base.rstrip("/") + "/upload", body,
                             {"Content-Type": "multipart/form-data; boundary=" + b})
try:
    print(urllib.request.urlopen(req, timeout=120).read().decode())
except urllib.error.HTTPError as e:
    print("Server error", e.code, e.read().decode())
except Exception as e:
    print("Could not reach server:", e)
