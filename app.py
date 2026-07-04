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
 
 
def monthly_counts(df: pd.DataFrame) -> pd.DataFrame:
    """Counts per month per theme, for the time-series chart. Rows without a
    parseable date are dropped."""
    d = df.dropna(subset=["date"]).copy()
    if d.empty:
        return pd.DataFrame(columns=["month", "theme", "count"])
    d["month"] = d["date"].dt.to_period("M").dt.to_timestamp()
    g = (d.groupby(["month", "theme"]).size().reset_index(name="count"))
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
 
    # ---- Sidebar filters ----
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
    mc = monthly_counts(fdf)
    if mc.empty:
        st.caption("No dated rows in the current selection.")
    else:
        chart = (alt.Chart(mc).mark_bar().encode(
            x=alt.X("month:T", title="Year"),
            y=alt.Y("count:Q", title="Articles"),
            color=alt.Color("theme:N", title="Theme"),
            tooltip=["month:T", "theme:N", "count:Q"],
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
