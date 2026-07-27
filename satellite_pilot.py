# ============================================================
# satellite_pilot.py — Espinar NDVI Pilot
#
# Downloads Landsat data from the Microsoft Planetary Computer for the
# Espinar conflict area (Cusco, Peru) and computes monthly NDVI from
# Jan 2023 to Jan 2025. Method adapted from Hernando's La Mamiña
# scripts (multi_index_analysis_microsoft.py).
#
# One-time setup:
#   pip install pystac-client planetary-computer stackstac rioxarray \
#               geopandas matplotlib pandas --break-system-packages
#
# Run:
#   python3 satellite_pilot.py
#
# Outputs (in outputs/satellite_pilot/, safe to delete anytime):
#   espinar_YYYY_MM_ndvi.tif   one NDVI map per month
#   ndvi_timeseries.csv        mean NDVI inside the polygon, per month
#   ndvi_timeseries.png        the time-series plot
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

# ----------------------------- CONFIG -----------------------------
# The conflict area, by GADM ID (from combined.csv, gadm_id column).
# PER.8.8.4_1 = Espinar district, Espinar province, Cusco, Peru.
GADM_ID = "PER.8.8.4_1"
GADM_FILE = Path("shapefiles/gadm41_PER.gpkg")

# Time window: Jan 2023 through Jan 2025 (conflict reported Jan 2024).
START_YEAR, START_MONTH = 2023, 1
END_YEAR, END_MONTH = 2025, 1

CLOUD_MAX = 80            # max % cloud cover per scene
MAX_SCENES_PER_MONTH = 10
RES_DEG = 30 / 111_320    # ~30 m pixels, in degrees
OUT_DIR = Path("outputs/satellite_pilot")
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
# ------------------------------------------------------------------


def load_conflict_polygon():
    """Load the conflict area's polygon from the GADM geopackage.

    The admin level is read off the GADM ID itself: PER.8_1 is level 1,
    PER.8.8_1 level 2, PER.8.8.4_1 level 3."""
    if not GADM_FILE.exists():
        raise SystemExit(f"GADM file not found: {GADM_FILE} — run this from "
                         "the project folder (same place as run_all.py).")
    level = GADM_ID.split("_")[0].count(".")
    layers = gpd.list_layers(GADM_FILE)["name"].tolist()
    layer = next(L for L in layers if L.endswith(f"_{level}"))
    gdf = gpd.read_file(GADM_FILE, layer=layer).to_crs(4326)
    match = gdf[gdf[f"GID_{level}"] == GADM_ID]
    if match.empty:
        raise SystemExit(f"GADM ID {GADM_ID} not found in layer {layer}.")
    name_cols = [c for c in (f"NAME_{level}", "NAME_2", "NAME_1") if c in match.columns]
    label = str(match.iloc[0][name_cols[0]]) if name_cols else GADM_ID
    print(f"📍 Area: {label} ({GADM_ID}, admin level {level})")
    return match


def month_range():
    """All (year, month) pairs from the start to the end month, inclusive."""
    y, m = START_YEAR, START_MONTH
    while (y, m) <= (END_YEAR, END_MONTH):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def infer_band_names(item):
    """Map generic band roles to this Landsat item's asset names
    (from Hernando's script)."""
    assets = set(item.assets.keys())
    band_map = {}
    for b in ["red", "SR_B3", "B3"]:
        if b in assets:
            band_map["RED"] = b
            break
    for b in ["nir08", "SR_B4", "B4"]:
        if b in assets:
            band_map["NIR"] = b
            break
    return band_map


def compute_monthly_ndvi(bbox, year, month):
    """Median-composite all usable Landsat scenes for one month and
    return an NDVI raster (or None if no scenes)."""
    catalog = pystac_client.Client.open(STAC_URL, modifier=pc.sign_inplace)

    last_day = pd.Timestamp(year=year, month=month, day=1).days_in_month
    dt = f"{year}-{month:02d}-01/{year}-{month:02d}-{last_day}"
    search = catalog.search(
        collections=["landsat-c2-l2"], bbox=bbox, datetime=dt,
        query={"eo:cloud_cover": {"lt": CLOUD_MAX}},
    )
    items = list(search.items())[:MAX_SCENES_PER_MONTH]
    if not items:
        print(f"  ⚠️  {year}-{month:02d}: no usable scenes")
        return None

    band_map = infer_band_names(items[0])
    if "RED" not in band_map or "NIR" not in band_map:
        print(f"  ⚠️  {year}-{month:02d}: missing RED/NIR bands")
        return None

    ds = stackstac.stack(
        items, assets=list(band_map.values()), bounds=bbox,
        resolution=RES_DEG, epsg=4326, dtype="float64",
        fill_value=np.nan, chunksize=1024, rescale=False,
    )
    inv = {v: k for k, v in band_map.items()}
    ds = ds.assign_coords(band=[inv.get(b, b) for b in ds.band.values])
    ds = ds * 0.0000275 - 0.2          # Landsat Collection 2 scale factors

    median = ds.median("time", skipna=True).compute()
    r = median.sel(band="RED")
    n = median.sel(band="NIR")
    ndvi = (n - r) / (n + r)
    return ndvi.rio.write_crs(4326)


def main():
    print("🛰️  Espinar NDVI pilot — Microsoft Planetary Computer")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    aoi = load_conflict_polygon()
    minx, miny, maxx, maxy = aoi.total_bounds
    bbox = (minx - 0.02, miny - 0.02, maxx + 0.02, maxy + 0.02)

    records = []
    months = list(month_range())
    for i, (year, month) in enumerate(months, 1):
        tif = OUT_DIR / f"espinar_{year}_{month:02d}_ndvi.tif"
        if tif.exists():
            print(f"✓ [{i}/{len(months)}] {year}-{month:02d} already downloaded.")
        else:
            # Retry up to 3 times per month (from Hernando's script).
            for attempt in range(1, 4):
                print(f"📊 [{i}/{len(months)}] {year}-{month:02d} "
                      f"(attempt {attempt}/3)...")
                try:
                    ndvi = compute_monthly_ndvi(bbox, year, month)
                    if ndvi is not None:
                        clipped = ndvi.rio.clip(aoi.geometry, aoi.crs)
                        clipped.rio.to_raster(tif, compress="LZW")
                        print(f"  ✅ saved {tif.name}")
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"  ❌ failed: {e}")
                    if attempt < 3:
                        time.sleep(5 * attempt)
                    else:
                        print(f"  ⛔ skipping {year}-{month:02d}.")

        # Mean NDVI inside the polygon for the time series.
        if tif.exists():
            da = rioxarray.open_rasterio(tif, masked=True)
            records.append({"year": year, "month": month,
                            "date": f"{year}-{month:02d}",
                            "mean_ndvi": float(np.nanmean(da.values))})

    if not records:
        raise SystemExit("No months downloaded — check your internet "
                         "connection and try again.")

    df = pd.DataFrame(records)
    df.to_csv(OUT_DIR / "ndvi_timeseries.csv", index=False)

    # ---- Plot ----
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(df["date"], df["mean_ndvi"], marker="o", color="forestgreen")
    ax.axvline("2024-01", color="firebrick", linestyle="--",
               label="Conflict reported (Jan 2024)")
    ax.set_title("Mean NDVI — Espinar, Cusco, Peru")
    ax.set_ylabel("NDVI (vegetation greenness)")
    ax.set_xlabel("Month")
    ax.tick_params(axis="x", rotation=60)
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "ndvi_timeseries.png", dpi=150)
    print(f"\n✅ DONE — {len(df)} months. See {OUT_DIR}/ndvi_timeseries.png")


if __name__ == "__main__":
    main()
