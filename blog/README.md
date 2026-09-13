# Building on AWS Lambda MicroVMs: the article series

Nine articles, written for the AWS Builder Center in series mode. Each markdown file starts with a front matter block holding the Builder Center form fields, followed by the article body ready to paste.

## Series metadata

| Field | Value |
|---|---|
| Series name | Building on AWS Lambda MicroVMs |
| Series cover | [img/cover-series.png](img/cover-series.png) (1200 x 675) |
| Canonical URL | leave empty; the articles are first published on Builder Center |

## Articles in order

| Part | File | Title | Cover |
|---|---|---|---|
| 1 | [00-control-and-scale-microvms-like-a-pro.md](00-control-and-scale-microvms-like-a-pro.md) | Control and scale AWS Lambda MicroVMs with microvm-ctl | [cover-00.png](img/cover-00.png) |
| 2 | [01-code-sandbox.md](01-code-sandbox.md) | Build a code execution sandbox on AWS Lambda MicroVMs | [cover-01.png](img/cover-01.png) |
| 3 | [02-ai-code-runner.md](02-ai-code-runner.md) | Run model-written code safely: an AI code runner on AWS Lambda MicroVMs | [cover-02.png](img/cover-02.png) |
| 4 | [03-agent-eval.md](03-agent-eval.md) | Evaluate agents on a fleet of identical AWS Lambda MicroVMs | [cover-03.png](img/cover-03.png) |
| 5 | [04-notebook.md](04-notebook.md) | A stateful notebook kernel that suspends for free on AWS Lambda MicroVMs | [cover-04.png](img/cover-04.png) |
| 6 | [05-data-analytics.md](05-data-analytics.md) | Sandboxed data analytics with DuckDB on AWS Lambda MicroVMs | [cover-05.png](img/cover-05.png) |
| 7 | [06-ci-runner.md](06-ci-runner.md) | Ephemeral CI runners on AWS Lambda MicroVMs: a fresh VM for every job | [cover-06.png](img/cover-06.png) |
| 8 | [07-pdf-service.md](07-pdf-service.md) | An HTML to PDF service on AWS Lambda MicroVMs that sleeps between bursts | [cover-07.png](img/cover-07.png) |
| 9 | [08-multi-tenant-agents.md](08-multi-tenant-agents.md) | Multi-tenant AI agents with one AWS Lambda MicroVM per tenant | [cover-08.png](img/cover-08.png) |

## Publishing checklist, per article

1. Open the markdown file. Copy `title` into the Title field and `description` into the Description field.
2. Copy everything below the closing `---` of the front matter into the Body. Paragraphs are single lines with a blank line between them, so nothing needs re-wrapping.
3. Upload the article's cover PNG from `img/` (1200 x 675, well under the 2 MB limit).
4. Add the five `tags` from the front matter.
5. Under Series, pick Building on AWS Lambda MicroVMs (create it once with the series cover).
6. Images in the body use absolute raw.githubusercontent.com URLs, so they render as soon as this repo is public. If the editor rejects remote images, upload the same PNGs from `img/` through the editor and replace the URLs.
7. Cross-references to other parts are plain text ("part 3 of this series") rather than hyperlinks, because Builder Center links only resolve between published articles. Add links after each article is live if you want them.

## Style rules the series follows

- Every number is measured on the live service in us-east-1, and the transcripts in `img/demo-*.png` are the recordings.
- One paragraph per line, plain ASCII punctuation, no decorative unicode outside literal terminal output.
- Diagrams are PNGs under `img/`, rendered with headless Chrome from the mermaid sources in `img/src/`. The part 1 diagram is hand-drawn SVG, shared with the microvm-ctl docs.
