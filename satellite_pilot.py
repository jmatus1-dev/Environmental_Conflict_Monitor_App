# ============================================================
# satellite_pilot.py — Multi-Indicator Satellite Analysis
#
# Downloads Landsat data from the Microsoft Planetary Computer for a
# conflict area and computes 8 monthly indices. Method adapted from
# Hernando's La Mamiña scripts (multi_index_analysis_microsoft.py).
#
# Two ways to use it:
#   1. Standalone (the Espinar pilot, same as always):
#        python3 satellite_pilot.py
#   2. From the app: app.py imports run_analysis() and calls it with
#      the conflict/dates chosen in Admin Mode.
#
# Indices (all derived from Landsat surface reflectance):
#   NDVI   — vegetation greenness
#   EVI2   — canopy structure (2-band EVI)
#   SAVI   — soil-adjusted vegetation / biomass
#   NDMI   — vegetation moisture
#   NDWI   — surface water (McFeeters)
#   MNDWI  — water bodies (modified NDWI)
#   NDBI   — built-up / urbanization
#   BSI    — bare soil
#
# One-time setup:
#   pip install pystac-client planetary-computer stackstac rioxarray \
#               geopandas matplotlib pandas --break-system-packages
#
# Outputs (in the chosen output folder):
#   <prefix>_YYYY_MM_<index>.tif   one map per month per index
#   indices_timeseries.csv         mean values inside the polygon, per month
#   <index>_timeseries.png         one plot per index (8 total)
# ============================================================

import os
import time
import warnings
warnings.filterwarnings("ignore")

# --- Network resilience settings (from Hernando's script) ---
os.environ["GDAL_HTTP_MAX_RETRY"] = "5"
os.environ["GDAL_HTTP_RETRY_DELAY"] = "2"
os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = "EMPTY_DIR"
os.environ["VSI_CACHE"] = "TRUE"
os.environ["VSI_CACHE_SIZE"] = "50000000"

from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import rioxarray  # noqa: F401  (registers the .rio accessor on DataArrays)
import stackstac
import pystac_client
import planetary_computer as pc

# ------------------------- SHARED SETTINGS -------------------------
CLOUD_MAX = 80            # max % cloud cover per scene
MAX_SCENES_PER_MONTH = 10
RES_DEG = 30 / 111_320    # ~30 m pixels, in degrees
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
GADM_DIR = Path("shapefiles")

# Bands we need. Friendly asset names are consistent across Landsat 4-9
# on the Planetary Computer STAC, so we prefer those.
BANDS = ["BLUE", "GREEN", "RED", "NIR", "SWIR1"]

# The 8 indices, each with a plot title, y-axis label, and line color.
# The `compute` function takes a dict {band: DataArray} and returns the
# index DataArray.
INDICES = {
    "ndvi":  {"title": "NDVI (vegetation greenness)",
              "color": "forestgreen",
              "compute": lambda b: (b["NIR"] - b["RED"]) / (b["NIR"] + b["RED"])},
    "evi2":  {"title": "EVI2 (canopy structure)",
              "color": "seagreen",
              "compute": lambda b: 2.5 * (b["NIR"] - b["RED"]) /
                                   (b["NIR"] + 2.4 * b["RED"] + 1.0)},
    "savi":  {"title": "SAVI (soil-adjusted vegetation / biomass)",
              "color": "olivedrab",
              # L = 0.5 is the standard mid-range soil adjustment.
              "compute": lambda b: ((b["NIR"] - b["RED"]) /
                                    (b["NIR"] + b["RED"] + 0.5)) * 1.5},
    "ndmi":  {"title": "NDMI (vegetation moisture)",
              "color": "teal",
              "compute": lambda b: (b["NIR"] - b["SWIR1"]) / (b["NIR"] + b["SWIR1"])},
    "ndwi":  {"title": "NDWI (surface water — McFeeters)",
              "color": "steelblue",
              "compute": lambda b: (b["GREEN"] - b["NIR"]) / (b["GREEN"] + b["NIR"])},
    "mndwi": {"title": "MNDWI (water bodies)",
              "color": "royalblue",
              "compute": lambda b: (b["GREEN"] - b["SWIR1"]) / (b["GREEN"] + b["SWIR1"])},
    "ndbi":  {"title": "NDBI (built-up / urbanization)",
              "color": "sienna",
              "compute": lambda b: (b["SWIR1"] - b["NIR"]) / (b["SWIR1"] + b["NIR"])},
    "bsi":   {"title": "BSI (bare soil)",
              "color": "chocolate",
              "compute": lambda b: (((b["SWIR1"] + b["RED"]) - (b["NIR"] + b["BLUE"])) /
                                    ((b["SWIR1"] + b["RED"]) + (b["NIR"] + b["BLUE"])))},
}
# ------------------------------------------------------------------


def load_conflict_polygon(gadm_id, gadm_dir=GADM_DIR, log=print):
    """Load a conflict area's polygon from its country's GADM geopackage.

    The country file is derived from the ID's prefix (PER... -> gadm41_PER.gpkg)
    and the admin level is read off the GADM ID itself: PER.8_1 is level 1,
    PER.8.8_1 level 2, PER.8.8.4_1 level 3."""
    country_code = gadm_id.split(".")[0].split("_")[0]
    gadm_file = Path(gadm_dir) / f"gadm41_{country_code}.gpkg"
    if not gadm_file.exists():
        raise RuntimeError(
            f"GADM file not found: {gadm_file} — run this from the project "
            "folder (same place as run_all.py), and run "
            "`python3 download_shapefiles.py` first if the shapefiles "
            "folder is missing.")
    level = gadm_id.split("_")[0].count(".")
    layers = gpd.list_layers(gadm_file)["name"].tolist()
    layer = next(L for L in layers if L.endswith(f"_{level}"))
    gdf = gpd.read_file(gadm_file, layer=layer).to_crs(4326)
    match = gdf[gdf[f"GID_{level}"] == gadm_id]
    if match.empty:
        raise RuntimeError(f"GADM ID {gadm_id} not found in layer {layer}.")
    name_cols = [c for c in (f"NAME_{level}", "NAME_2", "NAME_1") if c in match.columns]
    label = str(match.iloc[0][name_cols[0]]) if name_cols else gadm_id
    log(f"📍 Area: {label} ({gadm_id}, admin level {level})")
    return match, label


def month_range(start_year, start_month, end_year, end_month):
    """All (year, month) pairs from the start to the end month, inclusive."""
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def infer_band_names(item):
    """Map generic band roles to this Landsat item's asset names.

    Prefers the friendly names (blue, green, red, nir08, swir16) that
    are consistent across Landsat 4-9 on the Planetary Computer STAC.
    Falls back to SR_B* naming per generation:
      - Landsat 8/9 (OLI):     SR_B2/3/4/5/6 = BLUE/GREEN/RED/NIR/SWIR1
      - Landsat 4-7 (TM/ETM+): SR_B1/2/3/4/5 = BLUE/GREEN/RED/NIR/SWIR1"""
    assets = set(item.assets.keys())
    candidates = {
        "BLUE":  ["blue",   "SR_B2", "SR_B1"],
        "GREEN": ["green",  "SR_B3", "SR_B2"],
        "RED":   ["red",    "SR_B4", "SR_B3"],
        "NIR":   ["nir08",  "SR_B5", "SR_B4"],
        "SWIR1": ["swir16", "SR_B6", "SR_B5"],
    }
    band_map = {}
    for role, names in candidates.items():
        for n in names:
            if n in assets:
                band_map[role] = n
                break
    return band_map


def compute_monthly_indices(bbox, year, month, log=print):
    """Median-composite all usable Landsat scenes for one month and
    return a dict {index_name: DataArray} for the 8 indices.
    Returns None if no scenes or missing bands."""
    catalog = pystac_client.Client.open(STAC_URL, modifier=pc.sign_inplace)

    last_day = pd.Timestamp(year=year, month=month, day=1).days_in_month
    dt = f"{year}-{month:02d}-01/{year}-{month:02d}-{last_day}"
    search = catalog.search(
        collections=["landsat-c2-l2"], bbox=bbox, datetime=dt,
        query={"eo:cloud_cover": {"lt": CLOUD_MAX}},
    )
    items = list(search.items())[:MAX_SCENES_PER_MONTH]
    if not items:
        log(f"  ⚠️  {year}-{month:02d}: no usable scenes")
        return None

    band_map = infer_band_names(items[0])
    missing = [b for b in BANDS if b not in band_map]
    if missing:
        log(f"  ⚠️  {year}-{month:02d}: missing bands {missing}")
        return None

    ds = stackstac.stack(
        items, assets=[band_map[b] for b in BANDS], bounds=bbox,
        resolution=RES_DEG, epsg=4326, dtype="float64",
        fill_value=np.nan, chunksize=1024, rescale=False,
    )
    inv = {v: k for k, v in band_map.items()}
    ds = ds.assign_coords(band=[inv.get(b, b) for b in ds.band.values])
    ds = ds * 0.0000275 - 0.2          # Landsat Collection 2 scale factors

    median = ds.median("time", skipna=True).compute()
    bands = {b: median.sel(band=b) for b in BANDS}

    results = {}
    for name, spec in INDICES.items():
        arr = spec["compute"](bands)
        results[name] = arr.rio.write_crs(4326)
    return results


def run_analysis(gadm_id, start_year, start_month, end_year, end_month,
                 out_dir, prefix="area", area_title=None,
                 article_date=None, article_label=None,
                 gadm_dir=GADM_DIR, log=print):
    """Run the full multi-indicator analysis for one conflict area.

    Parameters
    ----------
    gadm_id       : GADM ID of the area, e.g. "PER.8.8.4_1"
    start/end     : time window, inclusive, as (year, month) numbers
    out_dir       : where tifs / CSV / plots get written
    prefix        : filename prefix for the tifs, e.g. "espinar"
    area_title    : plot title text, e.g. "Espinar, Cusco, Peru"
                    (defaults to the GADM area name)
    article_date  : "YYYY-MM" of the source article, for the dashed line
                    (None = no dashed line)
    article_label : legend text for the dashed line
    log           : where progress messages go (print for Terminal;
                    the app passes its own so messages show on screen)

    Returns a dict: {"csv": path, "plots": [paths], "months_done": int}
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log("🛰️  Multi-indicator analysis — Microsoft Planetary Computer")

    aoi, area_name = load_conflict_polygon(gadm_id, gadm_dir, log)
    if area_title is None:
        area_title = area_name
    minx, miny, maxx, maxy = aoi.total_bounds
    bbox = (minx - 0.02, miny - 0.02, maxx + 0.02, maxy + 0.02)

    def tif_path(year, month, name):
        return out_dir / f"{prefix}_{year}_{month:02d}_{name}.tif"

    def all_tifs_exist(year, month):
        return all(tif_path(year, month, name).exists() for name in INDICES)

    records = []
    months = list(month_range(start_year, start_month, end_year, end_month))
    if not months:
        raise RuntimeError("Empty time window — end must not be before start.")

    for i, (year, month) in enumerate(months, 1):
        if all_tifs_exist(year, month):
            log(f"✓ [{i}/{len(months)}] {year}-{month:02d} — all 8 tifs exist.")
        else:
            # Retry up to 3 times per month (from Hernando's script).
            for attempt in range(1, 4):
                log(f"📊 [{i}/{len(months)}] {year}-{month:02d} "
                    f"(attempt {attempt}/3)...")
                try:
                    idx_arrays = compute_monthly_indices(bbox, year, month, log)
                    if idx_arrays is not None:
                        for name, arr in idx_arrays.items():
                            clipped = arr.rio.clip(aoi.geometry, aoi.crs)
                            clipped.rio.to_raster(tif_path(year, month, name),
                                                  compress="LZW")
                        log(f"  ✅ saved 8 index tifs for {year}-{month:02d}")
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"  ❌ failed: {e}")
                    if attempt < 3:
                        time.sleep(5 * attempt)
                    else:
                        log(f"  ⛔ skipping {year}-{month:02d}.")

        # Mean value inside the polygon for each index, if we have it.
        row = {"year": year, "month": month, "date": f"{year}-{month:02d}"}
        for name in INDICES:
            tif = tif_path(year, month, name)
            if tif.exists():
                da = rioxarray.open_rasterio(tif, masked=True)
                row[f"mean_{name}"] = float(np.nanmean(da.values))
            else:
                row[f"mean_{name}"] = np.nan
        records.append(row)

    df = pd.DataFrame(records)
    months_done = int(df[[f"mean_{n}" for n in INDICES]]
                      .notna().any(axis=1).sum())
    if months_done == 0:
        raise RuntimeError("No months downloaded — check your internet "
                           "connection and try again.")

    csv_path = out_dir / "indices_timeseries.csv"
    df.to_csv(csv_path, index=False)

    # ---- Plots (one per index) ----
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_paths = []
    for name, spec in INDICES.items():
        col = f"mean_{name}"
        if df[col].isna().all():
            log(f"  ⚠️  no data for {name.upper()}, skipping plot")
            continue

        fig, ax = plt.subplots(figsize=(11, 5))
        ax.plot(df["date"], df[col], marker="o", color=spec["color"])
        # Dashed line only if the article date falls inside the window.
        if article_date and article_date in df["date"].values:
            ax.axvline(article_date, color="firebrick", linestyle="--",
                       label=article_label or f"Article publication "
                                              f"({article_date})")
            ax.legend()
        ax.set_title(f"Mean {name.upper()} — {area_title}")
        ax.set_ylabel(spec["title"])
        ax.set_xlabel("Month")
        ax.tick_params(axis="x", rotation=60)
        ax.grid(alpha=0.3)
        fig.tight_layout()
        png = out_dir / f"{name}_timeseries.png"
        fig.savefig(png, dpi=150)
        plt.close(fig)
        plot_paths.append(png)

    log(f"\n✅ DONE — {months_done} months with data, "
        f"{len(plot_paths)} plots. See {out_dir}/")
    return {"csv": csv_path, "plots": plot_paths, "months_done": months_done}


def main():
    """Standalone mode: the original Espinar pilot, unchanged behaviour."""
    run_analysis(
        gadm_id="PER.8.8.4_1",           # Espinar district, Cusco, Peru
        start_year=2023, start_month=1,
        end_year=2025, end_month=1,
        out_dir="outputs/satellite_pilot",
        prefix="espinar",
        area_title="Espinar, Cusco, Peru",
        article_date="2024-01",
        article_label="Article publication (Jan 2024)",
    )


if __name__ == "__main__":
    main()
