# Loaded by every Python process that has this directory on PYTHONPATH (labeling image only).
import os
if os.environ.get("BR_CMP_OUT"):
    import cmpprofile
    cmpprofile.install()
