"""
지도 뷰 — 시군구 코로플레스 지도
"""
import streamlit as st
import plotly.express as px
from data_loader import load_merged, get_geojson, SECTORS, percentile_rank

st.set_page_config(page_title="지도 뷰", layout="wide")
st.title("지도 뷰")

gdf = load_merged()
geojson = get_geojson(gdf)

# 지수 선택
INDEX_OPTIONS = {
    '생활인프라 편리성 종합지수': 'infra_idx',
    **{s['label']: f'conv_{s["col"]}' for s in SECTORS},
    **{f'공급수준 — {s["name"]}': f'sup_{s["col"]}' for s in SECTORS},
    **{f'향유수준 — {s["name"]}': f'pop_{s["col"]}' for s in SECTORS},
    **{f'충족수준 — {s["name"]}': f'acc_{s["col"]}' for s in SECTORS},
}

col_left, col_right = st.columns([1, 3])

with col_left:
    selected_label = st.selectbox("표시 지수 선택", list(INDEX_OPTIONS.keys()))
    col = INDEX_OPTIONS[selected_label]

    color_scheme = st.selectbox("색상 스케일", ['RdYlGn', 'Blues', 'Reds', 'YlOrRd', 'Viridis'])
    opacity = st.slider("투명도", 0.3, 1.0, 0.75, 0.05)

    st.markdown("---")
    st.subheader("전국 통계")
    s = gdf[col].dropna()
    st.metric("평균", f"{s.mean():.2f}")
    st.metric("중앙값", f"{s.median():.2f}")
    st.metric("최댓값", f"{s.max():.2f}")
    st.metric("최솟값", f"{s.min():.2f}")

with col_right:
    fig = px.choropleth_mapbox(
        gdf,
        geojson=geojson,
        locations='sgg_cd',
        featureidkey='properties.sgg_cd',
        color=col,
        color_continuous_scale=color_scheme,
        mapbox_style='carto-positron',
        zoom=6,
        center={'lat': 36.5, 'lon': 127.8},
        opacity=opacity,
        hover_name='sgg_nm_k',
        hover_data={col: ':.2f', 'sido_nm_k': True},
        labels={col: selected_label, 'sido_nm_k': '광역시도'},
        title=selected_label,
    )
    fig.update_layout(
        height=650,
        margin=dict(l=0, r=0, t=40, b=0),
        coloraxis_colorbar=dict(title=selected_label[:8]),
    )
    st.plotly_chart(fig, use_container_width=True)

# 클릭된 시군구 상세 (hover info 아래 순위 표)
st.markdown("---")
st.subheader(f"순위 상위 20 — {selected_label}")
rank_df = (
    gdf[['sgg_nm_k', 'sido_nm_k', col]]
    .dropna()
    .sort_values(col, ascending=False)
    .reset_index(drop=True)
)
rank_df.index += 1
rank_df.columns = ['시군구', '광역시도', selected_label]
st.dataframe(rank_df.head(20), use_container_width=True)
