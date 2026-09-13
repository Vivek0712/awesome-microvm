"""Series + article covers, 1200x675, tile motif for a fleet of microVMs."""
import random, pathlib
OUT = pathlib.Path(__file__).parent
W, H = 1200, 675
ARTICLES = {
    "00": "control plane", "01": "code sandbox", "02": "AI code runner", "03": "agent eval fleet",
    "04": "notebook kernel", "05": "data analytics", "06": "CI runners", "07": "HTML to PDF", "08": "multi-tenant agents",
}
RUN, SUS, OFF, HOT = "#34d399", "#60a5fa", "#334155", "#f472b6"

def tiles(seed, cols=14, rows=7, size=52, gap=14, x0=None, y0=None, hot=None):
    rnd = random.Random(seed)
    gw = cols * size + (cols - 1) * gap
    gh = rows * size + (rows - 1) * gap
    x0 = (W - gw) / 2 if x0 is None else x0
    y0 = (H - gh) / 2 - 30 if y0 is None else y0
    out = []
    for r in range(rows):
        for c in range(cols):
            p = rnd.random()
            fill = RUN if p < 0.42 else SUS if p < 0.72 else OFF
            op = 0.95 if fill != OFF else 0.55
            if hot and (r, c) == hot:
                fill, op = HOT, 1.0
            x, y = x0 + c * (size + gap), y0 + r * (size + gap)
            out.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{size}" height="{size}" rx="10" fill="{fill}" opacity="{op}"/>')
            if fill == HOT:
                out.append(f'<rect x="{x-6:.0f}" y="{y-6:.0f}" width="{size+12}" height="{size+12}" rx="14" fill="none" stroke="{HOT}" stroke-width="3" opacity="0.7"/>')
    return "\n".join(out)

def svg(seed, number=None, label=None):
    hot = (3, 5) if number else None
    grid = tiles(seed, cols=11, x0=90, hot=hot) if number else tiles(seed, hot=hot)
    body = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0b1220"/><stop offset="1" stop-color="#111c33"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.45" r="0.6">
      <stop offset="0" stop-color="#1e3a8a" stop-opacity="0.55"/><stop offset="1" stop-color="#0b1220" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <rect width="{W}" height="{H}" fill="url(#glow)"/>
  <g opacity="0.9">{grid}</g>
  <rect x="0" y="{H-104}" width="{W}" height="104" fill="#0b1220" opacity="0.88"/>
  <text x="48" y="{H-58}" font-size="18" fill="#94a3b8" letter-spacing="3">SERIES</text>
  <text x="48" y="{H-26}" font-size="30" font-weight="700" fill="#f8fafc">Building on AWS Lambda MicroVMs</text>
  <text x="{W-48}" y="{H-58}" font-size="24" font-weight="700" fill="#f8fafc" text-anchor="end">Vivek Raja</text>
  <text x="{W-48}" y="{H-34}" font-size="15" fill="#cbd5e1" text-anchor="end">AWS AI Hero  |  Sr. Solutions Architect, Aivar</text>
  <text x="{W-48}" y="{H-14}" font-size="13" fill="#94a3b8" text-anchor="end">AWS Partner Company</text>'''
    if number:
        body += f'''
  <text x="{W-48}" y="118" font-size="112" font-weight="800" fill="#f8fafc" text-anchor="end" opacity="0.95">{number}</text>
  <text x="{W-48}" y="156" font-size="26" fill="#cbd5e1" text-anchor="end">{label}</text>'''
    return body + "\n</svg>\n"

(OUT / "cover-series.svg").write_text(svg(7))
for n, label in ARTICLES.items():
    (OUT / f"cover-{n}.svg").write_text(svg(100 + int(n), n, label))
print("wrote", len(ARTICLES) + 1, "covers")
