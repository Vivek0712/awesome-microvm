"""Regenerate the paste-ready Builder Center bundle from the three series articles.

    python3 blog/publish/make_bundle.py

Bodies get a visible marker at every figure position instead of markdown image
syntax, because Builder Center only accepts uploads. The marker keeps spaces
inside the angle brackets so no markdown renderer treats it as a tag or autolink.
"""
import pathlib
import re

BLOG = pathlib.Path(__file__).resolve().parent.parent
PARTS = [(1, "00-control-and-scale.md"), (2, "01-seven-workloads.md"), (3, "02-multi-tenant-agents.md"),
         (4, "03-handoff.md")]

for n, f in PARTS:
    text = (BLOG / f).read_text()
    fm, body = text.split("\n---\n", 1)
    get = lambda k: re.search(rf'^{k}: (.*)$', fm, re.M).group(1).strip('"')
    imgs = re.findall(r'!\[[^\]]*\]\(img/([^)]+)\)', body)
    total, counter, placed = len(imgs), [0], []

    def marker(m: re.Match) -> str:
        counter[0] += 1
        heading = [h for h in re.findall(r'^#{2,3} (.*)$', body[:m.start()], re.M)] or ["(top)"]
        placed.append((counter[0], m.group(2), heading[-1]))
        return f"< FIGURE {counter[0]} of {total}: upload blog/img/{m.group(2)} here >\nAlt text: {m.group(1)}"

    body = re.sub(r'!\[([^\]]*)\]\(img/([^)]+)\)', marker, body)
    fields = (
        f"# Part {n}: form fields\n\n"
        f"Title ({len(get('title'))} chars):\n{get('title')}\n\n"
        f"Description ({len(get('description'))} chars):\n{get('description')}\n\n"
        f"Tags: {get('tags')}\nSeries: {get('series')}\nCover image: blog/{get('cover')}\n\n"
        "Figures, in body order. Each < FIGURE n of N > marker in the body is replaced by the upload; the\n"
        "Alt text line under it goes into the image's alt field; the italic line after it is the caption.\n"
        + "".join(f"  {i}. blog/img/{im}  (under \"{h}\")\n" for i, im, h in placed)
    )
    (BLOG / "publish" / f"part-{n}-fields.md").write_text(fields)
    (BLOG / "publish" / f"part-{n}-body.md").write_text(body.lstrip("\n"))
    print(f"part {n}: {len(imgs)} figure markers, {len(body.split())} words")
