"""Regenerate the paste-ready Builder Center bundle from the three series articles.

    python3 blog/publish/make_bundle.py

Bodies get a visible marker at every figure position instead of markdown image
syntax, because Builder Center only accepts uploads. The marker keeps spaces
inside the angle brackets so no markdown renderer treats it as a tag or autolink.
"""
import pathlib
import re

BLOG = pathlib.Path(__file__).resolve().parent.parent
PARTS = [(1, "00-control-and-scale.md"), (2, "01-seven-workloads.md"), (3, "02-multi-tenant-agents.md")]

for n, f in PARTS:
    text = (BLOG / f).read_text()
    fm, body = text.split("\n---\n", 1)
    get = lambda k: re.search(rf'^{k}: (.*)$', fm, re.M).group(1).strip('"')
    imgs = re.findall(r'!\[[^\]]*\]\(img/([^)]+)\)', body)
    body = re.sub(r'!\[([^\]]*)\]\(img/([^)]+)\)', r'< upload \2 here: \1 >', body)
    fields = (
        f"# Part {n}: form fields\n\n"
        f"Title ({len(get('title'))} chars):\n{get('title')}\n\n"
        f"Description ({len(get('description'))} chars):\n{get('description')}\n\n"
        f"Tags: {get('tags')}\nSeries: {get('series')}\nCover image: blog/{get('cover')}\n\n"
        "Images to upload, in body order (each replaces its < upload ... > marker in the body):\n"
        + "".join(f"  {i + 1}. blog/img/{im}\n" for i, im in enumerate(imgs))
    )
    (BLOG / "publish" / f"part-{n}-fields.md").write_text(fields)
    (BLOG / "publish" / f"part-{n}-body.md").write_text(body.lstrip("\n"))
    print(f"part {n}: {len(imgs)} figure markers, {len(body.split())} words")
