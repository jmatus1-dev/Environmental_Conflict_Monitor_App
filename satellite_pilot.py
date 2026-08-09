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
#   <index>_timeseries.png         one time-series plot per index (8 total)
#   <index>_map_before.png         map of the first available month
#   <index>_map_after.png          map of the last available month
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

# The 8 indices, each with a plot title, y-axis label, line color, and
# the matplotlib colormap used for the before/after maps. Colormaps are
# picked per indicator category so the maps read intuitively:
#   vegetation → red-to-green, greener = healthier
#   moisture   → brown-to-teal, teal = wetter
#   water      → blues
#   built-up   → oranges, darker = more built-up
#   bare soil  → yellow-orange-brown, darker = more bare
# The `compute` function takes a dict {band: DataArray} and returns the
# index DataArray.
INDICES = {
    "ndvi":  {"title": "NDVI (vegetation greenness)",
              "color": "forestgreen",
              "cmap":  "RdYlGn",
              "compute": lambda b: (b["NIR"] - b["RED"]) / (b["NIR"] + b["RED"])},
    "evi2":  {"title": "EVI2 (canopy structure)",
              "color": "seagreen",
              "cmap":  "RdYlGn",
              "compute": lambda b: 2.5 * (b["NIR"] - b["RED"]) /
                                   (b["NIR"] + 2.4 * b["RED"] + 1.0)},
    "savi":  {"title": "SAVI (soil-adjusted vegetation / biomass)",
              "color": "olivedrab",
              "cmap":  "YlGn",
              # L = 0.5 is the standard mid-range soil adjustment.
              "compute": lambda b: ((b["NIR"] - b["RED"]) /
                                    (b["NIR"] + b["RED"] + 0.5)) * 1.5},
    "ndmi":  {"title": "NDMI (vegetation moisture)",
              "color": "teal",
              "cmap":  "BrBG",
              "compute": lambda b: (b["NIR"] - b["SWIR1"]) / (b["NIR"] + b["SWIR1"])},
    "ndwi":  {"title": "NDWI (surface water — McFeeters)",
              "color": "steelblue",
              "cmap":  "Blues",
              "compute": lambda b: (b["GREEN"] - b["NIR"]) / (b["GREEN"] + b["NIR"])},
    "mndwi": {"title": "MNDWI (water bodies)",
              "color": "royalblue",
              "cmap":  "Blues",
              "compute": lambda b: (b["GREEN"] - b["SWIR1"]) / (b["GREEN"] + b["SWIR1"])},
    "ndbi":  {"title": "NDBI (built-up / urbanization)",
              "color": "sienna",
              "cmap":  "Oranges",
              "compute": lambda b: (b["SWIR1"] - b["NIR"]) / (b["SWIR1"] + b["NIR"])},
    "bsi":   {"title": "BSI (bare soil)",
              "color": "chocolate",
              "cmap":  "YlOrBr",
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


def _render_map(arr, aoi, title, cmap, vmin, vmax, cbar_label, out_path):
    """Render one indicator map to a PNG.

    Uses a shared vmin/vmax across the before/after pair so the visual
    comparison is honest — same colour = same value across both maps.
    The AOI polygon is drawn on top in black for context."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 7))
    arr.plot(
        ax=ax, cmap=cmap, vmin=vmin, vmax=vmax,
        add_colorbar=True,
        cbar_kwargs={"label": cbar_label},
    )
    aoi.boundary.plot(ax=ax, edgecolor="black", linewidth=1)
    ax.set_title(title)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


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

    Returns a dict:
      {
        "csv":          path to indices_timeseries.csv,
        "plots":        list of time-series PNG paths (kept for
                        backwards compatibility),
        "months_done":  number of months where any data was downloaded,
        "indicator_results": list of dicts, one per indicator, each with:
          {
            "name":              short key, e.g. "ndvi",
            "title":             human-readable title,
            "timeseries_path":   path to the time series PNG,
            "before_path":       path to the "first month" map PNG,
                                 or None if not available,
            "before_label":      "YYYY-MM" of the first-month map,
            "after_path":        path to the "last month" map PNG,
                                 or None if not available,
            "after_label":       "YYYY-MM" of the last-month map,
          }
      }
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

    # ---- Time-series plots + before/after maps (one row per index) ----
    # For the maps we use the first and last months in the window that
    # actually have all 8 tifs on disk. If the user's start/end month has
    # no cloud-free data, we fall back to the next/previous available
    # month, and record which one we used in the map label so it's
    # honest about what the picture shows.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    months_with_data = [(y, m) for (y, m) in months if all_tifs_exist(y, m)]
    if len(months_with_data) >= 2:
        before_key = months_with_data[0]
        after_key = months_with_data[-1]
    else:
        before_key = after_key = None
        log("  ⚠️  fewer than 2 months with data — skipping before/after maps.")

    plot_paths = []
    indicator_results = []
    for name, spec in INDICES.items():
        col = f"mean_{name}"
        result = {
            "name": name,
            "title": spec["title"],
            "timeseries_path": None,
            "before_path": None,
            "before_label": None,
            "after_path": None,
            "after_label": None,
        }

        if df[col].isna().all():
            log(f"  ⚠️  no data for {name.upper()}, skipping plots")
            indicator_results.append(result)
            continue

        # ---- Time series ----
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
        ts_png = out_dir / f"{name}_timeseries.png"
        fig.savefig(ts_png, dpi=150)
        plt.close(fig)
        plot_paths.append(ts_png)
        result["timeseries_path"] = ts_png

        # ---- Before / after maps ----
        if before_key and after_key and before_key != after_key:
            b_tif = tif_path(*before_key, name)
            a_tif = tif_path(*after_key, name)
            if b_tif.exists() and a_tif.exists():
                try:
                    b_arr = rioxarray.open_rasterio(b_tif, masked=True).squeeze()
                    a_arr = rioxarray.open_rasterio(a_tif, masked=True).squeeze()

                    # Shared colour scale across both maps so the visual
                    # comparison isn't lying. Percentile clip (2nd–98th)
                    # to keep a few outlier pixels from washing out the
                    # colour ramp for everything else.
                    joint = np.concatenate([
                        b_arr.values[np.isfinite(b_arr.values)].ravel(),
                        a_arr.values[np.isfinite(a_arr.values)].ravel(),
                    ])
                    if joint.size == 0:
                        log(f"  ⚠️  {name.upper()}: no finite pixels, "
                            f"skipping maps")
                    else:
                        vmin, vmax = np.percentile(joint, [2, 98])
                        if vmax <= vmin:
                            vmax = vmin + 1e-6

                        b_label = f"{before_key[0]}-{before_key[1]:02d}"
                        a_label = f"{after_key[0]}-{after_key[1]:02d}"

                        b_png = out_dir / f"{name}_map_before.png"
                        a_png = out_dir / f"{name}_map_after.png"

                        _render_map(
                            b_arr, aoi,
                            title=f"{name.upper()} — Before "
                                  f"({b_label}) — {area_title}",
                            cmap=spec.get("cmap", "viridis"),
                            vmin=vmin, vmax=vmax,
                            cbar_label=name.upper(),
                            out_path=b_png,
                        )
                        _render_map(
                            a_arr, aoi,
                            title=f"{name.upper()} — After "
                                  f"({a_label}) — {area_title}",
                            cmap=spec.get("cmap", "viridis"),
                            vmin=vmin, vmax=vmax,
                            cbar_label=name.upper(),
                            out_path=a_png,
                        )
                        result["before_path"] = b_png
                        result["before_label"] = b_label
                        result["after_path"] = a_png
                        result["after_label"] = a_label
                except Exception as e:  # noqa: BLE001
                    log(f"  ⚠️  {name.upper()}: map render failed ({e})")

        indicator_results.append(result)

    n_map_pairs = sum(1 for r in indicator_results if r["before_path"])
    log(f"\n✅ DONE — {months_done} months with data, "
        f"{len(plot_paths)} time series, "
        f"{n_map_pairs} before/after map pairs. See {out_dir}/")
    return {
        "csv": csv_path,
        "plots": plot_paths,
        "months_done": months_done,
        "indicator_results": indicator_results,
    }


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
