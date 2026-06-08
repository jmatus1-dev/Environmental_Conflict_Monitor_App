
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
    for col in ["country", "sector", "event_type", "environmental_issue",
                "severity", "urgency", "article_title", "article_url",
                "source", "llm_summary", "source_text_excerpt", "latitude",
                "longitude", "date_published", "geocode_precision"]:
        if col not in df.columns:
            df[col] = ""
 
    df["date"] = pd.to_datetime(df["date_published"], errors="coerce")
    df["lat"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["lon"] = pd.to_numeric(df["longitude"], errors="coerce")
    df["theme"] = df["sector"].where(df["sector"].str.strip() != "",
                                     df["event_type"])
    df["summary_display"] = df["llm_summary"].where(
        df["llm_summary"].str.strip() != "", df["source_text_excerpt"])
    return df
 
 
def apply_filters(df: pd.DataFrame, countries=None, sectors=None, events=None,
                  date_range=None, query: str = "") -> pd.DataFrame:
    """Return the subset of `df` matching the selected filters. Empty/None
    filter values mean 'no constraint'."""
    out = df
    if countries:
        out = out[out["country"].isin(countries)]
    if sectors:
        out = out[out["sector"].isin(sectors)]
    if events:
        out = out[out["event_type"].isin(events)]
    if date_range and len(date_range) == 2 and date_range[0] and date_range[1]:
        start, end = pd.to_datetime(date_range[0]), pd.to_datetime(date_range[1])
        out = out[(out["date"] >= start) & (out["date"] <= end)]
    if query:
        q = query.strip().lower()
        hay = (out["article_title"].str.lower() + " "
               + out["summary_display"].str.lower() + " "
               + out["environmental_issue"].str.lower())
        out = out[hay.str.contains(q, regex=False, na=False)]
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
    for _, r in mapped.iterrows():
        color = SECTOR_COLORS.get(r["sector"], "gray")
        title = (r["article_title"] or "")[:120]
        summary = (r["summary_display"] or "")[:300]
        url = r["article_url"]
        date_str = r["date"].date().isoformat() if pd.notna(r["date"]) else "unknown date"
        popup_html = (
            f"<b>{_esc(title)}</b><br>"
            f"<small>{_esc(r['source'])} &middot; {date_str} &middot; "
            f"{_esc(r['country'])}</small><br>"
            f"<i>{_esc(r['sector'])} / {_esc(r['event_type'])}"
            f"{(' / severity: ' + _esc(r['severity'])) if r['severity'] else ''}</i>"
            f"<p>{_esc(summary)}</p>"
            f"<a href='{_esc(url)}' target='_blank'>Read source &rarr;</a>"
        )
        folium.Marker(
            location=[r["lat"], r["lon"]],
            popup=folium.Popup(popup_html, max_width=320),
            tooltip=title,
            icon=folium.Icon(color=color, icon="info-sign"),
        ).add_to(marker_layer)
        heat_points.append([r["lat"], r["lon"]])
 
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
    query = st.sidebar.text_input("Search title / summary / issue")
 
    def opts(col):
        return sorted(v for v in df[col].unique() if str(v).strip())
 
    countries = st.sidebar.multiselect("Country", opts("country"))
    sectors = st.sidebar.multiselect("Theme / sector", opts("sector"))
    events = st.sidebar.multiselect("Event type", opts("event_type"))
 
    dated = df.dropna(subset=["date"])
    if not dated.empty:
        dmin, dmax = dated["date"].min().date(), dated["date"].max().date()
        date_range = st.sidebar.slider("Date range", min_value=dmin,
                                       max_value=dmax, value=(dmin, dmax))
    else:
        date_range = None
        st.sidebar.caption("No parseable dates yet for a date filter.")
 
    fdf = apply_filters(df, countries, sectors, events, date_range, query)
 
    # ---- Headline metrics ----
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Conflicts shown", len(fdf))
    c2.metric("Countries", fdf["country"].replace("", pd.NA).nunique())
    c3.metric("With coordinates", int(fdf[["lat", "lon"]].notna().all(axis=1).sum()))
    if not fdf.dropna(subset=["date"]).empty:
        dd = fdf.dropna(subset=["date"])
        c4.metric("Date span", f"{dd['date'].min().date()} -> {dd['date'].max().date()}")
    else:
        c4.metric("Date span", "n/a")
 
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
            x=alt.X("month:T", title="Month"),
            y=alt.Y("count:Q", title="Articles"),
            color=alt.Color("theme:N", title="Theme"),
            tooltip=["month:T", "theme:N", "count:Q"],
        ).properties(height=280))
        st.altair_chart(chart, use_container_width=True)
 
    # ---- Table ----
    st.subheader("Articles")
    table_cols = ["date_published", "source", "country", "region_department",
                  "sector", "event_type", "environmental_issue", "severity",
                  "urgency", "article_title", "article_url"]
    table_cols = [c for c in table_cols if c in fdf.columns]
    st.dataframe(
        fdf[table_cols].sort_values("date_published", ascending=False),
        use_container_width=True, hide_index=True,
        column_config={"article_url": st.column_config.LinkColumn("source url")},
    )
 
 
if __name__ == "__main__":
    main()
