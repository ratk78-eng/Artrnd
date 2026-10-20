"""Cuts the views out of the uploaded drawings and prints their boxes (for eyeballing the crops)."""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ui"))
from drawing import segment_views  # noqa: E402

out_root = os.path.join(os.environ["TEMP"], "kompasmcp-segment")
for path in sys.argv[1:]:
    t = time.time()
    views = segment_views(path, os.path.join(out_root, os.path.basename(os.path.dirname(path)) + "_" +
                                             os.path.splitext(os.path.basename(path))[0]))
    print(path, f"{time.time() - t:.1f}s", [(v["file"], v["box"]) for v in views])
