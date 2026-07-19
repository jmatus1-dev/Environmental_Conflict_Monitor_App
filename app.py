"""
app.py - Step 4: the Streamlit dashboard.
 
Reads the single combined CSV produced by the pipeline (scrape -> enrich ->
geocode) and renders:
  * an interactive map with clustered, colour-coded markers; each popup shows
    the conflict summary and a link to the source article;
  * a toggleable heatmap layer showing conflict hotspots;
  * sidebar filters for country, theme/sector, event type and date range, plus
    a free-text search box;
  * headline metrics and a time-series chart of conflict frequency over time;
  * a download button that exports the current filtered selection as CSV;
  * the filtered data as a table.

It also has an ADMIN MODE (sidebar toggle). When on, an extra "Setup & Run"
view appears where an admin configures a new satellite analysis: time frame,
conflict location (from the mapped database OR manual coordinates),
extractive industry, and report language. The Run Analysis button is a shell
for now — the Phase 2 satellite pipeline it will eventually trigger has not
been built yet.

It degrades gracefully: if you open it before running the LLM/geocoding steps,
the charts, filters and table still work; only the map needs coordinates and
it'll tell you how many rows have them.
 
Setup
-----
    pip install streamlit folium streamlit-folium pandas
 
Run
---
    streamlit run app.py
"""
 
from __future__ import annotations
 
import os
from datetime import date, timedelta
import pandas as pd
 
DATA_PATH = os.environ.get("ENVCONFLICT_DATA", os.path.join("data", "combined.csv"))
 
# Stable colours per sector for the map markers / legend.
SECTOR_COLORS = {
    "mining": "red",
    "oil_gas": "black",
    "logging_deforestation": "darkgreen",
    "infrastructure": "blue",
    "agriculture": "orange",
    "protected_areas": "purple",
    "other": "gray",
    "": "lightgray",
}

# The clean admin columns produced by match_admin.py / finalize_columns.py.
COUNTRY_COL = "country (admin 0)"
REGION_COL = "region (admin 1)"
MUNI_COL = "municipality (admin 2)"
SUBDIST_COL = "sub-district (admin 3)"

# Human-readable display names for the coded sector / event values, so the
# filters, popups and table don't show raw strings like "logging_deforestation".
SECTOR_LABELS = {
    "mining": "Mining",
    "oil_gas": "Oil & Gas",
    "logging_deforestation": "Logging / Deforestation",
    "infrastructure": "Infrastructure",
    "agriculture": "Agriculture",
    "protected_areas": "Protected Areas",
    "other": "Other",
    "": "Unclassified",
}
EVENT_LABELS = {
    "pollution": "Pollution",
    "enforcement_action": "Enforcement Action",
    "violence": "Violence",
    "legal": "Legal Action",
    "consultation_dispute": "Consultation Dispute",
    "protest": "Protest",
    "displacement": "Displacement",
    "": "Unclassified",
}


def pretty_sector(v: str) -> str:
    v = (v or "").strip()
    return SECTOR_LABELS.get(v, v.replace("_", " ").title())


def pretty_event(v: str) -> str:
    v = (v or "").strip()
    return EVENT_LABELS.get(v, v.replace("_", " ").title())


# Rough precision ranking, so when a conflict's articles have different
# coordinates we put the single marker on the most precise one.
_PRECISION_RANK = {
    "town": 3, "village": 3, "suburb": 3, "quarter": 3, "hamlet": 3,
    "locality": 3, "amenity": 3, "office": 3, "tourism": 3,
    "feature": 2, "region": 1, "country": 0,
}
 
 
# ---------------------------------------------------------------------------
# Data loading + filtering  (pure pandas; unit-testable without Streamlit)
# ---------------------------------------------------------------------------
 
def load_data(path: str = DATA_PATH) -> pd.DataFrame:
    """Load the combined CSV into a tidy DataFrame. Adds parsed `date`,
    numeric `lat`/`lon`, a `theme` column (sector, falling back to event type),
    and a `summary_display` column (LLM summary, falling back to the scraped
    excerpt). Missing columns are tolerated so the app works at any pipeline
    stage."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
 
    # Guarantee the columns the app reads exist, even pre-enrichment.
    for col in [COUNTRY_COL, REGION_COL, MUNI_COL, SUBDIST_COL, "country",
                "sector", "event_type", "environmental_issue",
                "article_title", "article_url", "source", "llm_summary",
                "source_text_excerpt", "latitude", "longitude",
                "date_published", "geocode_precision", "conflict_id",
                "coverage_count"]:
        if col not in df.columns:
            df[col] = ""
 
    df["date"] = pd.to_datetime(df["date_published"], errors="coerce")
    # coverage_count is written by conflict_ids.py; default to 1 if absent.
    df["coverage"] = pd.to_numeric(df["coverage_count"], errors="coerce").fillna(1).astype(int)
    df["lat"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["lon"] = pd.to_numeric(df["longitude"], errors="coerce")
    df["theme"] = df["sector"].where(df["sector"].str.strip() != "",
                                     df["event_type"])
    df["summary_display"] = df["llm_summary"].where(
        df["llm_summary"].str.strip() != "", df["source_text_excerpt"])
    return df
 
 
def apply_filters(df: pd.DataFrame, countries=None, sectors=None, events=None,
                  date_range=None) -> pd.DataFrame:
    """Return the subset of `df` matching the selected filters. Empty/None
    filter values mean 'no constraint'."""
    out = df
    if countries:
        out = out[out[COUNTRY_COL].isin(countries)]
    if sectors:
        out = out[out["sector"].isin(sectors)]
    if events:
        out = out[out["event_type"].isin(events)]
    if date_range and len(date_range) == 2 and date_range[0] and date_range[1]:
        start, end = pd.to_datetime(date_range[0]), pd.to_datetime(date_range[1])
        out = out[(out["date"] >= start) & (out["date"] <= end)]
    return out
 
 
def yearly_counts(df: pd.DataFrame) -> pd.DataFrame:
    """Counts per YEAR per theme, for the time-series chart. Yearly (not
    monthly) buckets keep the chart readable across a multi-year span and when
    a sparse filter is applied. Rows without a parseable date are dropped, and
    the theme is given its human-readable label for the legend."""
    d = df.dropna(subset=["date"]).copy()
    if d.empty:
        return pd.DataFrame(columns=["year", "theme", "count"])
    d["year"] = d["date"].dt.year
    g = d.groupby(["year", "theme"]).size().reset_index(name="count")
    g["theme"] = g["theme"].map(pretty_sector)
    return g
 
 
# ---------------------------------------------------------------------------
# Map building (folium)
# ---------------------------------------------------------------------------
 
def build_map(df: pd.DataFrame):
    """Build a folium Map: a clustered marker layer (with rich popups) and a
    heatmap layer, toggleable via the layer control."""
    import folium
    from folium.plugins import HeatMap, MarkerCluster
 
    mapped = df.dropna(subset=["lat", "lon"])
    if mapped.empty:
        center = [-5.0, -65.0]  # Amazon basin-ish default
        zoom = 3
    else:
        center = [mapped["lat"].mean(), mapped["lon"].mean()]
        zoom = 4
 
    fmap = folium.Map(location=center, zoom_start=zoom, tiles="cartodbpositron")
 
    marker_layer = MarkerCluster(name="Conflict markers")
    heat_points = []
    # Group articles by conflict so ONE marker can link to all of them. Rows
    # with no conflict_id are each treated as their own single-article group.
    mapped = mapped.reset_index(drop=True)
    cid = mapped["conflict_id"].astype(str).str.strip()
    mapped["_gid"] = cid.where(cid != "", other="__row" + mapped.index.astype(str))

    for _gid, grp in mapped.groupby("_gid", sort=False):
        # Put the marker on the group's most precise coordinate.
        ranks = grp["geocode_precision"].map(
            lambda p: _PRECISION_RANK.get(str(p).strip().lower(), 2))
        rep_row = grp.loc[ranks.idxmax()]
        color = SECTOR_COLORS.get(rep_row["sector"], "gray")
        loc_bits = " &middot; ".join(
            _esc(x) for x in [rep_row[COUNTRY_COL], rep_row[REGION_COL]]
            if str(x).strip())
        sector_line = (f"{_esc(pretty_sector(rep_row['sector']))} / "
                       f"{_esc(pretty_event(rep_row['event_type']))}")
        n = len(grp)

        if n == 1:
            r0 = grp.iloc[0]
            date_str = (r0["date"].date().isoformat()
                        if pd.notna(r0["date"]) else "unknown date")
            popup_html = (
                f"<b>{_esc((r0['article_title'] or '')[:120])}</b><br>"
                f"<small>{_esc(r0['source'])} &middot; {date_str} &middot; "
                f"{loc_bits}</small><br>"
                f"<i>{sector_line}</i>"
                f"<p style='margin:6px 0'>"
                f"{_esc((r0['summary_display'] or '')[:280])}</p>"
                f"<a href='{_esc(r0['article_url'])}' target='_blank'>"
                f"Read source &rarr;</a>"
            )
        else:
            items = []
            for _, a in grp.sort_values("date", ascending=False).iterrows():
                d = (a["date"].date().isoformat()
                     if pd.notna(a["date"]) else "unknown date")
                items.append(
                    f"<li style='margin-bottom:6px'>"
                    f"<a href='{_esc(a['article_url'])}' target='_blank'>"
                    f"{_esc(a['source'])} &middot; {d}</a><br>"
                    f"<small>{_esc((a['article_title'] or '')[:90])}</small></li>"
                )
            popup_html = (
                f"<b>Conflict &middot; {n} articles</b><br>"
                f"<small>{loc_bits} &middot; <i>{sector_line}</i></small>"
                f"<ul style='padding-left:18px;margin:6px 0'>"
                f"{''.join(items)}</ul>"
            )

        tooltip = (rep_row["article_title"] or "")[:110]
        if n > 1:
            tooltip = f"{n} articles · {tooltip}"
        folium.Marker(
            location=[rep_row["lat"], rep_row["lon"]],
            popup=folium.Popup(popup_html, max_width=340),
            tooltip=tooltip,
            icon=folium.Icon(color=color, icon="info-sign"),
        ).add_to(marker_layer)
        heat_points.append([rep_row["lat"], rep_row["lon"]])
 
    marker_layer.add_to(fmap)
    if heat_points:
        HeatMap(heat_points, name="Hotspot heatmap", radius=18,
                blur=15, show=False).add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)
    return fmap, len(mapped)
 
 
def _esc(s) -> str:
    """Minimal HTML escaping for popup text."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("'", "&#39;"))
 
 
# ---------------------------------------------------------------------------
# Admin Mode: Setup & Run panel
# ---------------------------------------------------------------------------
#
# This is a UI shell for Phase 2. It captures the five inputs Hernando spec'd
# (time frame, location, industry, language) and validates them, but the
# button that would kick off the satellite pipeline just shows a "coming soon"
# message and echoes the captured configuration. Once the Planetary Computer
# pipeline and Quarto slide-deck generator are built, we swap that stub for
# a real subprocess call.
#
 
def render_admin_mode(df: pd.DataFrame) -> None:
    """Draw the admin panel where a user configures a new satellite analysis."""
    import streamlit as st

    st.header("Setup & Run — New Satellite Analysis")
    st.caption(
        "Configure a conflict location and time frame. When Phase 2 is wired "
        "up, clicking **Run Analysis** will download imagery from the "
        "Microsoft Planetary Computer, compute the nine environmental "
        "indicators, and produce a PDF slide deck."
    )

    # --- 1. Time frame -------------------------------------------------------
    st.subheader("1. Time frame")
    default_start = date.today() - timedelta(days=365 * 5)
    c1, c2 = st.columns(2)
    start_date = c1.date_input("Start date", value=default_start)
    end_date = c2.date_input("End date", value=date.today())

    # --- 2. Conflict location ------------------------------------------------
    st.subheader("2. Conflict location")
    loc_mode = st.radio(
        "Location source",
        ["Pick from mapped conflicts in the database",
         "Enter coordinates manually (unmapped conflict)"],
        label_visibility="collapsed",
    )

    latitude: float | None = None
    longitude: float | None = None
    location_label = ""
    conflict_id: str | None = None

    if loc_mode.startswith("Pick"):
        # Cascade dropdowns: Country -> Region -> Municipality.
        # Only rows with real coordinates AND a country are pickable.
        # The user can stop at any level; coordinates get assigned from the
        # most-precisely-geocoded row in whatever subset they've narrowed to.
        # (This matters because match_admin.py deliberately leaves deeper
        # admin fields blank when the geocode isn't precise enough — so many
        # conflicts have country + region but no municipality.)
        mapped = df.dropna(subset=["lat", "lon"]).copy()
        mapped = mapped[mapped[COUNTRY_COL].str.strip() != ""]

        if mapped.empty:
            st.warning(
                "No geocoded conflicts in the database yet. Run "
                "`python3 run_all.py` first so `geocode.py` can populate "
                "coordinates."
            )
        else:
            countries = sorted(mapped[COUNTRY_COL].unique())
            country = st.selectbox("Country", ["— select —"] + countries)

            region = "— select —"
            muni = "— select —"
            sub = None

            if country != "— select —":
                sub = mapped[mapped[COUNTRY_COL] == country]
                regions = sorted(
                    r for r in sub[REGION_COL].unique() if str(r).strip()
                )

                if regions:
                    region = st.selectbox(
                        "Region (admin 1)", ["— select —"] + regions
                    )
                else:
                    st.caption(
                        "No region-level data available for this country — "
                        "coordinates will be picked from the country level."
                    )

                if region != "— select —":
                    sub = sub[sub[REGION_COL] == region]
                    munis = sorted(
                        m for m in sub[MUNI_COL].unique() if str(m).strip()
                    )

                    if munis:
                        muni = st.selectbox(
                            "Municipality (admin 2)", ["— select —"] + munis
                        )
                    else:
                        st.caption(
                            "No municipality-level data available for this "
                            "region — coordinates will be picked from the "
                            "region level."
                        )

                    if muni != "— select —":
                        sub = sub[sub[MUNI_COL] == muni]

            # Assign coordinates at whatever depth the user reached. We pick
            # the row in the current subset with the most precise geocode
            # (via the same _PRECISION_RANK the map uses) so the coordinate
            # always points at a real, specific place.
            if sub is not None and not sub.empty:
                ranks = sub["geocode_precision"].map(
                    lambda p: _PRECISION_RANK.get(str(p).strip().lower(), 2)
                )
                row = sub.loc[ranks.idxmax()]
                latitude = float(row["lat"])
                longitude = float(row["lon"])
                conflict_id = str(row.get("conflict_id", "")) or None
                article_title = str(row.get("article_title", "") or "")[:80]

                if muni != "— select —":
                    depth = "municipality"
                    location_label = f"{muni}, {region}, {country}"
                elif region != "— select —":
                    depth = "region"
                    location_label = f"{region}, {country}"
                else:
                    depth = "country"
                    location_label = country

                st.success(
                    f"**{location_label}**  \n"
                    f"Coordinates: {latitude:.4f}, {longitude:.4f} "
                    f"(selected at *{depth}* level)  \n"
                    f"Source article: _{article_title or 'n/a'}_  \n"
                    f"Conflict ID: `{conflict_id or 'n/a'}`"
                )

                if depth == "country":
                    st.warning(
                        "Only country-level selection reached. The "
                        "coordinates above come from one specific article "
                        "and may be far from the conflict you have in mind. "
                        "Narrow down by region or municipality if you can, "
                        "or use the manual coordinate option instead."
                    )
    else:
        c1, c2 = st.columns(2)
        latitude = c1.number_input(
            "Latitude", value=0.0, format="%.6f",
            min_value=-90.0, max_value=90.0,
        )
        longitude = c2.number_input(
            "Longitude", value=0.0, format="%.6f",
            min_value=-180.0, max_value=180.0,
        )
        location_label = st.text_input(
            "Location label (optional)",
            placeholder="e.g. Cerrejón mine, La Guajira",
        )

    # --- 3. Industry ---------------------------------------------------------
    st.subheader("3. Extractive industry type")
    # Use the same coded sector list the rest of the app uses, minus the
    # empty/"other" catch-alls which don't make sense as an analysis target.
    industry_keys = [k for k in SECTOR_LABELS.keys() if k and k != "other"]
    industry = st.selectbox(
        "Industry",
        industry_keys,
        format_func=pretty_sector,
    )

    # --- 4. Language ---------------------------------------------------------
    st.subheader("4. Report language")
    language = st.radio(
        "Language", ["English", "Spanish"], horizontal=True,
        label_visibility="collapsed",
    )

    # --- 5. Run --------------------------------------------------------------
    st.subheader("5. Run analysis")
    run_clicked = st.button("▶  Run Analysis", type="primary")

    if run_clicked:
        # Validate before doing anything.
        errors = []
        if start_date >= end_date:
            errors.append("End date must be after start date.")
        if latitude is None or longitude is None:
            errors.append("Please pick or enter a location.")
        elif loc_mode.startswith("Enter") and latitude == 0.0 and longitude == 0.0:
            errors.append(
                "Coordinates (0, 0) look like a placeholder — "
                "please enter real values."
            )

        if errors:
            for e in errors:
                st.error(e)
        else:
            st.info(
                "🚧  **Coming soon.**  The satellite pipeline is not yet "
                "built. Once Phase 2 is wired up, this button will:\n\n"
                "1. Download Sentinel-2 and rainfall data from the Microsoft "
                "Planetary Computer for the selected date range and location\n"
                "2. Compute the nine indicators (NDVI, EVI2, SAVI, NDMI, "
                "NDWI, MNDWI, NDBI, BSI, and Rainfall)\n"
                "3. Generate a time-series chart and a map for each indicator\n"
                "4. Render a Quarto PDF slide deck in the chosen language"
            )

            with st.expander("Configuration captured", expanded=True):
                st.json({
                    "time_frame": {
                        "start_date": str(start_date),
                        "end_date": str(end_date),
                    },
                    "location": {
                        "mode": ("database" if loc_mode.startswith("Pick")
                                 else "manual"),
                        "label": location_label,
                        "latitude": latitude,
                        "longitude": longitude,
                        "conflict_id": conflict_id,
                    },
                    "industry": pretty_sector(industry),
                    "language": language,
                })


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------
 
def main() -> None:
    import streamlit as st
    import altair as alt
    from streamlit_folium import st_folium
 
    st.set_page_config(page_title="Environmental Conflict Monitor",
                       layout="wide")
    st.title("Environmental Conflict Monitor")
 
    if not os.path.exists(DATA_PATH):
        st.warning(f"No data file at `{DATA_PATH}`. Run `python run_all.py` "
                   "first (then optionally `enrich_llm.py` and `geocode.py`).")
        st.stop()
 
    df = load_data(DATA_PATH)

    # ---- Sidebar: Admin toggle + mode picker --------------------------------
    # When Admin is OFF the app looks exactly like the old viewer.
    # When Admin is ON a second radio option appears that swaps the main area
    # over to the Setup & Run panel.
    st.sidebar.header("Mode")
    is_admin = st.sidebar.toggle(
        "Admin Mode",
        value=False,
        help="Turn on to configure and launch a new satellite analysis.",
    )
    if is_admin:
        mode = st.sidebar.radio(
            "View",
            ["📊 View Results", "⚙️ Setup & Run"],
            label_visibility="collapsed",
        )
    else:
        mode = "📊 View Results"
    st.sidebar.markdown("---")

    # ---- Admin Setup & Run: replaces the whole main area --------------------
    if mode == "⚙️ Setup & Run":
        render_admin_mode(df)
        return

    # ---- View Results (the original viewer, unchanged) ---------------------
    st.sidebar.header("Filters")
 
    def opts(col):
        return sorted(v for v in df[col].unique() if str(v).strip())
 
    countries = st.sidebar.multiselect("Country", opts(COUNTRY_COL))
    sectors = st.sidebar.multiselect("Sector", opts("sector"),
                                     format_func=pretty_sector)
    events = st.sidebar.multiselect("Event type", opts("event_type"),
                                    format_func=pretty_event)
 
    dated = df.dropna(subset=["date"])
    if not dated.empty:
        dmin, dmax = dated["date"].min().date(), dated["date"].max().date()
        date_range = st.sidebar.slider("Date range", min_value=dmin,
                                       max_value=dmax, value=(dmin, dmax))
    else:
        date_range = None
        st.sidebar.caption("No parseable dates yet for a date filter.")
 
    fdf = apply_filters(df, countries, sectors, events, date_range)
 
    # ---- Headline metrics ----
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Articles shown", len(fdf))
    c2.metric("Unique conflicts", fdf["conflict_id"].replace("", pd.NA).nunique())
    c3.metric("Countries", fdf[COUNTRY_COL].replace("", pd.NA).nunique())
    c4.metric("With coordinates", int(fdf[["lat", "lon"]].notna().all(axis=1).sum()))
    if not fdf.dropna(subset=["date"]).empty:
        dd = fdf.dropna(subset=["date"])
        c5.metric("Date span", f"{dd['date'].min().date()} -> {dd['date'].max().date()}")
    else:
        c5.metric("Date span", "n/a")
 
    # ---- Download button (exports whatever the user has currently filtered) ----
    csv_bytes = fdf.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="Download filtered data as CSV",
        data=csv_bytes,
        file_name="environmental_conflicts_filtered.csv",
        mime="text/csv",
        help="Downloads the current filtered selection shown below.",
    )
 
    # ---- Map ----
    st.subheader("Map")
    fmap, n_mapped = build_map(fdf)
    if n_mapped == 0:
        st.info("No rows in the current selection have coordinates yet. "
                "Run `python geocode.py` to place events on the map.")
    st_folium(fmap, use_container_width=True, height=520,
              returned_objects=[])
 
    # ---- Time series ----
    st.subheader("Conflict frequency over time")
    yc = yearly_counts(fdf)
    if yc.empty:
        st.caption("No dated rows in the current selection.")
    else:
        chart = (alt.Chart(yc).mark_bar().encode(
            x=alt.X("year:O", title="Year"),
            y=alt.Y("count:Q", title="Articles",
                    axis=alt.Axis(tickMinStep=1, format="d")),
            color=alt.Color("theme:N", title="Theme"),
            tooltip=[alt.Tooltip("year:O", title="Year"),
                     alt.Tooltip("theme:N", title="Theme"),
                     alt.Tooltip("count:Q", title="Articles")],
        ).properties(height=280))
        st.altair_chart(chart, use_container_width=True)
 
    # ---- Table ----
    st.subheader("Articles")
    show = fdf.copy()
    show["sector"] = show["sector"].map(pretty_sector)
    show["event_type"] = show["event_type"].map(pretty_event)
    table_cols = ["source", "article_title", "article_url", "date_published",
                  COUNTRY_COL, REGION_COL, MUNI_COL, SUBDIST_COL, "sector",
                  "event_type", "environmental_issue", "source_text_excerpt",
                  "coverage_count"]
    table_cols = [c for c in table_cols if c in show.columns]
    show = show.sort_values("date", ascending=False)
    st.dataframe(
        show[table_cols],
        use_container_width=True, hide_index=True,
        column_config={
            "source": "Source",
            "article_title": "Title",
            "article_url": st.column_config.LinkColumn("Source URL"),
            "date_published": "Date",
            COUNTRY_COL: "Country",
            REGION_COL: "Region",
            MUNI_COL: "Municipality",
            SUBDIST_COL: "Sub-district",
            "sector": "Sector",
            "event_type": "Event type",
            "environmental_issue": "Environmental issue",
            "source_text_excerpt": "Excerpt",
            "coverage_count": "Coverage",
        },
    )
 
 
if __name__ == "__main__":
    main()
