"""Command line: python -m edit_os <command> ...

  paragraphs IN.docx                       list paragraph IDs and text
  refs IN.docx                             list the reference entries found
  render IN.docx CHANGES.json OUTDIR       write tracked and clean copies from accepted changes
"""

from __future__ import annotations

import os
import sys

from . import docmodel, references, tracked
from .changes import ChangeSet


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, path = argv[0], argv[1]
    if cmd == "paragraphs":
        for p in docmodel.load(path).paragraphs:
            if p.text.strip():
                print(f"{p.id}\t{p.style or '-'}\t{p.text[:100]}")
    elif cmd == "refs":
        for r in references.extract(docmodel.load(path)):
            print(f"{r.para_id}\t{r.number if r.number is not None else '-'}\t{r.text}")
    elif cmd == "render" and len(argv) == 4:
        changes = ChangeSet.load(argv[2])
        os.makedirs(argv[3], exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        for kind, is_tracked in (("tracked", True), ("clean", False)):
            out = os.path.join(argv[3], f"{stem}_{kind}.docx")
            tracked.render(docmodel.load(path), changes, tracked=is_tracked).save(out)
            print(out)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
