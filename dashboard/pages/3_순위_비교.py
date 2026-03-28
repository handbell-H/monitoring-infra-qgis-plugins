"""
순위 비교 — 전국 순위 테이블 + 두 시군구 레이더 비교
"""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from data_loader import load_merged, SECTORS

st.set_page_config(page_title="순위 비교", layout="wide")
st.title("순위 비교")

gdf = load_merged()

# 지수 선택
INDEX_OPTIONS = {
    '생활인프라 편리성 종합지수': 'infra_idx',
    **{s['label']: f'conv_{s["col"]}' for s in SECTORS},
}

selected_label = st.selectbox("정렬 기준 지수", list(INDEX_OPTIONS.keys()))
col = INDEX_OPTIONS[selected_label]

col_left, col_right = st.columns([3, 2])

with col_left:
    st.subheader(f"전국 순위 — {selected_label}")

    rank_df = (
        gdf[['sido_nm_k', 'sgg_nm_k', 'infra_idx'] + [f'conv_{s["col"]}' for s in SECTORS]]
        .dropna(subset=[col])
        .sort_values(col, ascending=False)
        .reset_index(drop=True)
    )
    rank_df.index += 1
    rank_df.columns = (
        ['광역시도', '시군구', '종합지수'] + [s['name'] for s in SECTORS]
    )
    rank_df = rank_df.round(2)

    st.dataframe(
        rank_df.style.background_gradient(subset=['종합지수'], cmap='RdYlGn'),
        use_container_width=True,
        height=600,
    )

with col_right:
    st.subheader("두 시군구 비교")
    all_sgus = sorted(gdf['sgg_nm_k'].dropna().unique())

    sgg_a = st.selectbox("시군구 A", all_sgus, index=0, key='cmp_a')
    sgg_b = st.selectbox("시군구 B", all_sgus, index=min(1, len(all_sgus)-1), key='cmp_b')

    row_a = gdf[gdf['sgg_nm_k'] == sgg_a].iloc[0]
    row_b = gdf[gdf['sgg_nm_k'] == sgg_b].iloc[0]

    labels = [s['name'] for s in SECTORS]
    vals_a = [row_a[f'conv_{s["col"]}'] for s in SECTORS]
    vals_b = [row_b[f'conv_{s["col"]}'] for s in SECTORS]

    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(
        r=vals_a + [vals_a[0]],
        theta=labels + [labels[0]],
        fill='toself',
        name=sgg_a,
        line_color='royalblue',
    ))
    fig.add_trace(go.Scatterpolar(
        r=vals_b + [vals_b[0]],
        theta=labels + [labels[0]],
        fill='toself',
        name=sgg_b,
        line_color='tomato',
        opacity=0.5,
    ))
    fig.update_layout(
        polar=dict(radialaxis=dict(visible=True)),
        showlegend=True,
        height=400,
        margin=dict(l=40, r=40, t=40, b=40),
    )
    st.plotly_chart(fig, use_container_width=True)

    # 비교 수치표
    cmp_data = {
        '지표': ['종합지수'] + [s['name'] for s in SECTORS],
        sgg_a: [round(row_a['infra_idx'], 2)] + [round(row_a[f'conv_{s["col"]}'], 2) for s in SECTORS],
        sgg_b: [round(row_b['infra_idx'], 2)] + [round(row_b[f'conv_{s["col"]}'], 2) for s in SECTORS],
    }
    cmp_df = pd.DataFrame(cmp_data)
    cmp_df['차이 (A-B)'] = (cmp_df[sgg_a] - cmp_df[sgg_b]).round(2)
    st.dataframe(cmp_df, use_container_width=True, hide_index=True)
