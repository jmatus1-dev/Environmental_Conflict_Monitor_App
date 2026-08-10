# ============================================================
# report_slides.py — Quarto slide deck for a finished analysis
#
# Takes the outputs of satellite_pilot.run_analysis() (time-series
# plots + before/after maps in an output folder) and produces a PDF
# slide deck, following the structure of Hernando's
# bolivia-aula-conectada decks:
#
#   1. Title slide     "Environmental Assessment of the <Industry>
#                       Industry — <Area>", author AImpact Lab,
#                       today's date
#   2. Contents slide  three sections: Vegetation, Water, Soil
#   3. One slide per plot: for each indicator, a time-series slide
#      then a map slide (before/after side by side)
#   4. Closing slide   "Thank you!" + info@aimpactlab.com
#
# English and Spanish supported (the Admin form's language choice).
#
# How it works (same pipeline as Hernando's render_pdf.sh):
#   1. Write a .qmd file into the analysis output folder — plain
#      markdown + image includes, no embedded code, so nothing can
#      break at render time.
#   2. `quarto render` → reveal.js HTML.
#   3. decktape (if installed) or headless Chrome → PDF.
#
# One-time setup on your machine:
#   - Install Quarto:  https://quarto.org/docs/get-started/
#   - Chrome installed (for the HTML→PDF step), or optionally:
#       npm install -g @astefanutti/decktape
#
# Standalone usage (after an analysis has produced its outputs):
#   python3 report_slides.py outputs/analyses/PER_8_8_4_1 \
#       --industry "Mining" --area "Espinar, Cusco, Peru" --lang en
# ============================================================

import datetime as _dt
import shutil
import subprocess
from pathlib import Path

# The three sections of the deck and which indicators go in each.
# Order here = order of the slides.
SECTIONS = [
    ("vegetation", ["ndvi", "evi2", "savi", "ndmi"]),
    ("water",      ["ndwi", "mndwi"]),
    ("soil",       ["ndbi", "bsi"]),
]

# All user-facing deck text, per language.
TEXT = {
    "en": {
        "lang_code": "en",
        "title": "Environmental Assessment of the {industry} Industry",
        "subtitle": "{area}",
        "author": "AImpact Lab",
        "contents": "Contents",
        "sections": {"vegetation": "Vegetation", "water": "Water",
                     "soil": "Soil"},
        "timeseries": "{name} — time series",
        "maps": "{name} — before and after",
        "before": "Before ({label})",
        "after": "After ({label})",
        "thanks": "Thank you!",
        "footer": "Environmental Conflict Monitor · AImpact Lab",
        "indicator_names": {
            "ndvi": "NDVI (vegetation greenness)",
            "evi2": "EVI2 (canopy structure)",
            "savi": "SAVI (soil-adjusted vegetation)",
            "ndmi": "NDMI (vegetation moisture)",
            "ndwi": "NDWI (surface water)",
            "mndwi": "MNDWI (water bodies)",
            "ndbi": "NDBI (built-up areas)",
            "bsi": "BSI (bare soil)",
        },
    },
    "es": {
        "lang_code": "es",
        "title": "Evaluación Ambiental de la Industria: {industry}",
        "subtitle": "{area}",
        "author": "AImpact Lab",
        "contents": "Contenido",
        "sections": {"vegetation": "Vegetación", "water": "Agua",
                     "soil": "Suelo"},
        "timeseries": "{name} — serie de tiempo",
        "maps": "{name} — antes y después",
        "before": "Antes ({label})",
        "after": "Después ({label})",
        "thanks": "¡Gracias!",
        "footer": "Monitor de Conflictos Ambientales · AImpact Lab",
        "indicator_names": {
            "ndvi": "NDVI (verdor de la vegetación)",
            "evi2": "EVI2 (estructura del dosel)",
            "savi": "SAVI (vegetación ajustada por suelo)",
            "ndmi": "NDMI (humedad de la vegetación)",
            "ndwi": "NDWI (agua superficial)",
            "mndwi": "MNDWI (cuerpos de agua)",
            "ndbi": "NDBI (áreas construidas)",
            "bsi": "BSI (suelo desnudo)",
        },
    },
}

CONTACT_EMAIL = "info@aimpactlab.com"

# Where headless Chrome usually lives, per OS.
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
    "google-chrome",                                                 # Linux
    "chromium-browser",
    "chromium",
]


def _norm_lang(language):
    """'English'/'Spanish'/'en'/'es' → 'en' or 'es'."""
    s = str(language).strip().lower()
    return "es" if s.startswith(("es", "sp")) else "en"


def build_qmd(indicator_results, out_dir, industry_label, area_label,
              language="en", theme_source=None, log=print):
    """Write the deck's .qmd (and its theme) into the analysis folder.

    indicator_results : the list from run_analysis()'s return dict
    out_dir           : the analysis output folder (PNGs live here)
    industry_label    : e.g. "Mining"
    area_label        : e.g. "Espinar, Cusco, Peru"
    language          : "English"/"Spanish"/"en"/"es"
    theme_source      : path to slides_theme.scss (defaults to the one
                        next to this script)

    Returns the path of the written .qmd.
    """
    out_dir = Path(out_dir)
    lang = _norm_lang(language)
    t = TEXT[lang]

    # The theme file must sit next to the .qmd for Quarto to find it.
    if theme_source is None:
        theme_source = Path(__file__).parent / "slides_theme.scss"
    theme_dest = out_dir / "slides_theme.scss"
    if Path(theme_source).exists():
        shutil.copyfile(theme_source, theme_dest)
    else:
        log(f"⚠️  Theme file not found at {theme_source} — deck will use "
            f"the default Quarto theme.")

    today = _dt.date.today().isoformat()
    title = t["title"].format(industry=industry_label)
    subtitle = t["subtitle"].format(area=area_label)

    lines = [
        "---",
        f'title: "{title}"',
        f'subtitle: "{subtitle}"',
        f'author: "{t["author"]}"',
        f'date: "{today}"',
        "date-format: long",
        f'lang: {t["lang_code"]}',
        "format:",
        "  revealjs:",
    ]
    if theme_dest.exists():
        lines.append("    theme: [default, slides_theme.scss]")
    lines += [
        f'    footer: "{t["footer"]}"',
        "    slide-number: c/t",
        "    transition: fade",
        "    incremental: false",
        "    embed-resources: true",
        "    fig-align: center",
        "---",
        "",
    ]

    # ---- Contents slide ----
    lines += [f'## {t["contents"]}', ""]
    for key, _ in SECTIONS:
        lines.append(f'- {t["sections"][key]}')
    lines.append("")

    # ---- One section per theme, one slide per plot ----
    by_name = {r["name"]: r for r in indicator_results}
    for key, indicator_keys in SECTIONS:
        lines += [f'# {t["sections"][key]}', ""]
        for ind in indicator_keys:
            r = by_name.get(ind)
            if r is None:
                continue
            pretty = t["indicator_names"].get(ind, ind.upper())

            # Time-series slide.
            if r.get("timeseries_path"):
                ts = Path(r["timeseries_path"]).name
                lines += [
                    f'## {t["timeseries"].format(name=ind.upper())}',
                    "",
                    f"![{pretty}]({ts})",
                    "",
                ]

            # Map slide: before/after side by side.
            if r.get("before_path") and r.get("after_path"):
                b = Path(r["before_path"]).name
                a = Path(r["after_path"]).name
                b_cap = t["before"].format(label=r.get("before_label", ""))
                a_cap = t["after"].format(label=r.get("after_label", ""))
                lines += [
                    f'## {t["maps"].format(name=ind.upper())}',
                    "",
                    ":::: {.columns}",
                    "",
                    '::: {.column width="50%"}',
                    f"![{b_cap}]({b})",
                    ":::",
                    "",
                    '::: {.column width="50%"}',
                    f"![{a_cap}]({a})",
                    ":::",
                    "",
                    "::::",
                    "",
                ]

    # ---- Closing slide ----
    lines += [
        f'## {t["thanks"]}',
        "",
        f"[{CONTACT_EMAIL}](mailto:{CONTACT_EMAIL})",
        "",
    ]

    qmd_path = out_dir / f"report_slides_{lang}.qmd"
    qmd_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"📝 Wrote {qmd_path}")
    return qmd_path


def _find_chrome():
    for cand in CHROME_CANDIDATES:
        path = shutil.which(cand) or (cand if Path(cand).exists() else None)
        if path:
            return path
    return None


def render_pdf(qmd_path, log=print):
    """Render the .qmd to HTML with Quarto, then convert to PDF.

    Same strategy as Hernando's render_pdf.sh:
      decktape if installed, else headless Chrome.
    Returns the PDF path."""
    qmd_path = Path(qmd_path)
    html_path = qmd_path.with_suffix(".html")
    pdf_path = qmd_path.with_suffix(".pdf")

    if shutil.which("quarto") is None:
        raise RuntimeError(
            "Quarto is not installed (or not on the PATH). Install it "
            "from https://quarto.org/docs/get-started/ and try again."
        )

    log("🖥️  Rendering slides with Quarto...")
    proc = subprocess.run(
        ["quarto", "render", str(qmd_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Quarto render failed:\n{proc.stderr[-2000:]}")
    if not html_path.exists():
        raise RuntimeError(f"Quarto finished but {html_path} is missing.")

    # --- Path A: decktape (best quality) ---
    if shutil.which("decktape"):
        log("📄 Converting to PDF with decktape...")
        proc = subprocess.run(
            ["decktape", "reveal", "--size", "1280x720",
             str(html_path), str(pdf_path)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and pdf_path.exists():
            log(f"✅ PDF ready: {pdf_path}")
            return pdf_path
        log("⚠️  decktape failed, falling back to Chrome...")

    # --- Path B: headless Chrome ---
    chrome = _find_chrome()
    if chrome is None:
        raise RuntimeError(
            "Could not convert HTML to PDF — neither decktape nor Chrome "
            "was found. Install Chrome, or run "
            "`npm install -g @astefanutti/decktape`."
        )
    log("📄 Converting to PDF with headless Chrome...")
    proc = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu",
         "--run-all-compositor-stages-before-draw",
         "--virtual-time-budget=30000",
         "--no-pdf-header-footer",
         f"--print-to-pdf={pdf_path}",
         f"file://{html_path.resolve()}?print-pdf"],
        capture_output=True, text=True,
    )
    if not pdf_path.exists() or pdf_path.stat().st_size < 5000:
        raise RuntimeError(
            "The PDF came out blank — Chrome printed before the slides "
            "finished loading. Most reliable fix: "
            "`npm install -g @astefanutti/decktape` and generate again."
        )
    log(f"✅ PDF ready: {pdf_path}")
    return pdf_path


def build_deck(indicator_results, out_dir, industry_label, area_label,
               language="en", log=print):
    """One-call entry point for the app: qmd → HTML → PDF.

    Returns the PDF path."""
    qmd = build_qmd(indicator_results, out_dir, industry_label,
                    area_label, language, log=log)
    return render_pdf(qmd, log=log)


def main():
    """Standalone mode: build a deck from an existing analysis folder."""
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out_dir", help="Analysis output folder with the PNGs")
    ap.add_argument("--industry", default="Mining")
    ap.add_argument("--area", default="")
    ap.add_argument("--lang", default="en", choices=["en", "es"])
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    if not out_dir.exists():
        raise SystemExit(f"Folder not found: {out_dir}")

    # Reconstruct indicator_results from the files on disk, so this
    # works even without re-running the analysis.
    from satellite_pilot import INDICES
    indicator_results = []
    for name in INDICES:
        ts = out_dir / f"{name}_timeseries.png"
        b = out_dir / f"{name}_map_before.png"
        a = out_dir / f"{name}_map_after.png"
        indicator_results.append({
            "name": name,
            "title": name.upper(),
            "timeseries_path": ts if ts.exists() else None,
            "before_path": b if b.exists() else None,
            "before_label": "",
            "after_path": a if a.exists() else None,
            "after_label": "",
        })

    pdf = build_deck(indicator_results, out_dir, args.industry,
                     args.area, args.lang)
    print(f"\nDeck: {pdf}")


if __name__ == "__main__":
    main()
