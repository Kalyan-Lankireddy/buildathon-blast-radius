"""Download sqlglot at the pinned commit from GitHub and unpack the package and tests.

Runs at image build time only. The selector reads the files a patch touches from this copy.
"""
import io
import os
import sys
import tarfile
import urllib.request

COMMIT = sys.argv[1]
DEST = sys.argv[2]
URL = f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{COMMIT}"
KEEP = ("sqlglot/", "tests/")

with urllib.request.urlopen(URL, timeout=120) as response:
    data = response.read()
prefix = f"sqlglot-{COMMIT}/"
count = 0
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
    for member in tar.getmembers():
        if not member.isfile() or not member.name.startswith(prefix):
            continue
        rel = member.name[len(prefix):]
        if ".." in rel.split("/") or not rel.startswith(KEEP):
            continue
        target = os.path.join(DEST, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as out:
            out.write(tar.extractfile(member).read())
        count += 1
if count < 100 or not os.path.exists(os.path.join(DEST, "sqlglot", "__init__.py")):
    sys.exit(f"unexpected archive: {count} files")
print(f"unpacked {count} files from {COMMIT}")
