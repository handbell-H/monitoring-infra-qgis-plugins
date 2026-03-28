"""
분포 분석 — 히스토그램 · 박스플롯 · 산점도
"""
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
from data_loader import load_merged, SECTORS

st.set_page_config(page_title="분포 분석", layout="wide")
st.title("분포 분석")

gdf = load_merged()

INDEX_OPTIONS = {
    '생활인프라 편리성 종합지수': 'infra_idx',
    **{s['label']: f'conv_{s["col"]}' for s in SECTORS},
    **{f'공급수준 — {s["name"]}': f'sup_{s["col"]}' for s in SECTORS},
    **{f'향유수준 — {s["name"]}': f'pop_{s["col"]}' for s in SECTORS},
    **{f'충족수준 — {s["name"]}': f'acc_{s["col"]}' for s in SECTORS},
}

tab1, tab2, tab3 = st.tabs(["히스토그램", "박스플롯", "산점도"])

# ── Tab 1: 히스토그램 ──────────────────────────────────────────
with tab1:
    col1, col2 = st.columns([1, 3])
    with col1:
        sel = st.selectbox("지수 선택", list(INDEX_OPTIONS.keys()), key='hist_sel')
        col = INDEX_OPTIONS[sel]
        bins = st.slider("구간 수", 5, 50, 20)
        color_by_sido = st.checkbox("광역시도별 색상", value=False)

    with col2:
        if color_by_sido:
            fig = px.histogram(
                gdf.dropna(subset=[col]),
                x=col, color='sido_nm_k',
                nbins=bins,
                title=sel,
                labels={col: sel},
            )
        else:
            fig = px.histogram(
                gdf.dropna(subset=[col]),
                x=col,
                nbins=bins,
                title=sel,
                labels={col: sel},
                color_discrete_sequence=['steelblue'],
            )
        # 평균선
        mean_val = gdf[col].mean()
        fig.add_vline(x=mean_val, line_dash='dash', line_color='red',
                      annotation_text=f'전국평균 {mean_val:.1f}', annotation_position='top right')
        fig.update_layout(height=450)
        st.plotly_chart(fig, use_container_width=True)

    # 기술통계
    desc = gdf[col].describe().round(3)
    st.dataframe(desc.to_frame(sel).T, use_container_width=True)

# ── Tab 2: 박스플롯 ──────────────────────────────────────────
with tab2:
    col1, col2 = st.columns([1, 3])
    with col1:
        box_type = st.radio("박스플롯 유형", ['단일 지수 비교', '부문별 비교'])
        if box_type == '단일 지수 비교':
            sel_b = st.selectbox("지수 선택", list(INDEX_OPTIONS.keys()), key='box_sel')
            col_b = INDEX_OPTIONS[sel_b]
        group_by = st.checkbox("광역시도별 그룹", value=True)

    with col2:
        if box_type == '단일 지수 비교':
            if group_by:
                fig = px.box(
                    gdf.dropna(subset=[col_b]),
                    x='sido_nm_k', y=col_b,
                    color='sido_nm_k',
                    title=f'{sel_b} — 광역시도별 분포',
                    labels={col_b: sel_b, 'sido_nm_k': '광역시도'},
                )
            else:
                fig = px.box(
                    gdf.dropna(subset=[col_b]),
                    y=col_b,
                    title=f'{sel_b} 전국 분포',
                    color_discrete_sequence=['steelblue'],
                )
        else:
            # 5개 편리성 부문 나란히
            melt_df = gdf[['sgg_nm_k', 'sido_nm_k'] + [f'conv_{s["col"]}' for s in SECTORS]].melt(
                id_vars=['sgg_nm_k', 'sido_nm_k'],
                var_name='부문코드', value_name='편리성 지수'
            )
            sec_map = {f'conv_{s["col"]}': s['name'] for s in SECTORS}
            melt_df['부문'] = melt_df['부문코드'].map(sec_map)
            if group_by:
                fig = px.box(melt_df.dropna(subset=['편리성 지수']),
                             x='부문', y='편리성 지수', color='sido_nm_k',
                             title='부문별 편리성 분포 (광역시도)')
            else:
                fig = px.box(melt_df.dropna(subset=['편리성 지수']),
                             x='부문', y='편리성 지수', color='부문',
                             title='부문별 편리성 분포')
        fig.update_layout(height=500, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

# ── Tab 3: 산점도 ──────────────────────────────────────────
with tab3:
    col1, col2 = st.columns([1, 3])
    with col1:
        x_label = st.selectbox("X축", list(INDEX_OPTIONS.keys()), key='sc_x')
        y_label = st.selectbox("Y축", list(INDEX_OPTIONS.keys()),
                               index=min(1, len(INDEX_OPTIONS)-1), key='sc_y')
        x_col = INDEX_OPTIONS[x_label]
        y_col = INDEX_OPTIONS[y_label]
        color_sido = st.checkbox("광역시도 색상", value=True, key='sc_color')
        show_trend = st.checkbox("추세선", value=True)

    with col2:
        plot_df = gdf[['sgg_nm_k', 'sido_nm_k', x_col, y_col]].dropna()
        fig = px.scatter(
            plot_df,
            x=x_col, y=y_col,
            color='sido_nm_k' if color_sido else None,
            hover_name='sgg_nm_k',
            trendline='ols' if show_trend else None,
            labels={x_col: x_label, y_col: y_label, 'sido_nm_k': '광역시도'},
            title=f'{x_label} vs {y_label}',
            height=500,
        )
        fig.update_traces(marker=dict(size=6, opacity=0.7))
        fig.update_layout(showlegend=color_sido)
        st.plotly_chart(fig, use_container_width=True)

    if show_trend:
        corr = plot_df[x_col].corr(plot_df[y_col])
        st.caption(f"피어슨 상관계수: **{corr:.3f}**")
