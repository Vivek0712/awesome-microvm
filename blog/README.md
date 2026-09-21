# Building on AWS Lambda MicroVMs: the article series

Four articles, written for the AWS Builder Center in series mode. Each markdown file starts with a front matter block holding the Builder Center form fields, followed by the article body ready to paste. The long-form write-up of each example is kept under [deep-dives/](deep-dives/) for readers who want every detail; those are GitHub-only.

## Series metadata

| Field | Value |
|---|---|
| Series name | Building on AWS Lambda MicroVMs |
| Series cover | [img/cover-series.png](img/cover-series.png) (1200 x 675) |
| Author byline on covers | Vivek Raja, AWS AI Hero, Sr. Solutions Architect, Aivar (AWS Partner Company) |
| Canonical URL | leave empty; the articles are first published on Builder Center |

## Articles in order

| Part | File | Title | Cover | Published |
|---|---|---|---|---|
| 1 | [00-control-and-scale.md](00-control-and-scale.md) | Control and scale AWS Lambda MicroVMs with microvm-ctl | [cover-00.png](img/cover-00.png) | [live](https://builder.aws.com/content/3JIDTpz0ZgatSBv24drra3gEod9/control-and-scale-aws-lambda-microvms-with-microvm-ctl) |
| 2 | [01-seven-workloads.md](01-seven-workloads.md) | Seven workloads Lambda could never run, until MicroVMs | [cover-01.png](img/cover-01.png) | [live](https://builder.aws.com/content/3JJ2oNWY9EsZzivMMx044cSlrFQ/seven-workloads-lambda-could-never-run-until-microvms) |
| 3 | [02-multi-tenant-agents.md](02-multi-tenant-agents.md) | A kernel for every customer: scaling AI agents to 1,000 tenants on AWS Lambda MicroVMs with microvm-ctl | [cover-02.png](img/cover-02.png) | [live](https://builder.aws.com/content/3JJ7tASPSSUUTrcpWnWtxOuu8g3/a-kernel-for-every-customer-scaling-ai-agents-to-1000-tenants-on-aws-lambda-microvms-with-microvm-ctl) |
| 4 | [03-handoff.md](03-handoff.md) | Hand a task to a MicroVM from anywhere: one lease, Step Functions, durable functions, or your own controller | [cover-03.png](img/cover-03.png) | unpublished |

## Publishing checklist, per article

1. Open the markdown file. Copy `title` into the Title field and `description` into the Description field.
2. Copy everything below the closing `---` of the front matter into the Body. Paragraphs are single lines with a blank line between them, so nothing needs re-wrapping.
3. Upload the article's cover PNG from `img/` (1200 x 675, well under the 2 MB limit).
4. Add the five `tags` from the front matter.
5. Under Series, pick Building on AWS Lambda MicroVMs (create it once with the series cover).
6. Builder Center accepts uploaded images only, not remote or relative links. Paste the body from `publish/part-N-body.md`, which carries a visible `< upload <file> here: <caption> >` marker at every figure position; at each marker, upload that file from `blog/img/` through the editor's image button and delete the marker line. `publish/part-N-fields.md` lists the files in order. Regenerate both with `python3 blog/publish/make_bundle.py` after editing an article.
7. Cross-references to other parts are plain text ("part 2 of this series") rather than hyperlinks, because Builder Center links only resolve between published articles. Links to GitHub for the code are real hyperlinks and work as is.
8. All three are live (links in the table above) and the repository README links to them. Cross-references inside the articles now carry the live URLs too; re-paste a body from `publish/` if you want the links in the published version.

## Images to upload, per article

In body order. The cover goes in the cover field, not the body.

**00-control-and-scale.md**: cover `img/cover-00.png`

- `img/arch-00-plane.png`
- `img/mvm-image-ls.png`
- `img/benchmark.png`
- `img/lifecycle.png`
- `img/mvm-cost.png`
- `img/mvm-quotas.png`

**01-seven-workloads.md**: cover `img/cover-01.png`

- `img/arch-01-code-sandbox.png`
- `img/demo-code-sandbox.png`
- `img/demo-ai-code-runner.png`
- `img/arch-03-agent-eval.png`
- `img/demo-agent-eval.png`
- `img/demo-notebook.png`
- `img/arch-05-data-analytics.png`
- `img/demo-data-analytics.png`
- `img/demo-ci-runner.png`
- `img/demo-pdf-service.png`
- `img/mvm-cost.png`

**02-multi-tenant-agents.md**: cover `img/cover-02.png`

**03-handoff.md**: cover `img/cover-03.png`
- `img/playground-lease.png`
- `img/demo-sfn-terminate-failed.png`
- `img/demo-durable-hang.png`
- `img/arch-09-fanout-map.png`
- `img/playground-fanout.png`
- `img/handoff-bench.png`
- `img/fleet-watch.png`
- `img/demo-bench.png`
- `img/demo-playground-fleet-jobs.png`

- `img/arch-08-multi-tenant.png`
- `img/demo-multi-tenant-agents.png`
- `img/mvm-quotas.png`

## Style rules the series follows

- Every number is measured on the live service in us-east-1, and the transcripts in `img/demo-*.png` are the recordings.
- One paragraph per line, plain ASCII punctuation, no decorative unicode outside literal terminal output.
- Diagrams are PNGs under `img/`, rendered with headless Chrome from the mermaid sources in `img/src/`. The part 1 diagram is hand-drawn SVG, shared with the microvm-ctl docs.
- Covers are generated by `img/src/make_covers.py`.

## Credits

The series was inspired by [lambda-microvm-starter](https://github.com/vidanov/lambda-microvm-starter) by Alexey Vidanov. Parts 1 and 3 credit it in the body.
