"""
시군구 상세 — 레이더 차트 + 부문별 막대 차트
"""
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from data_loader import load_merged, SECTORS, percentile_rank

st.set_page_config(page_title="시군구 상세", layout="wide")
st.title("시군구 상세")

gdf = load_merged()

# 시도 → 시군구 cascade 선택
sido_list = sorted(gdf['sido_nm_k'].dropna().unique())
sido_sel = st.selectbox("광역시도 선택", sido_list)
sgg_list = sorted(gdf.loc[gdf['sido_nm_k'] == sido_sel, 'sgg_nm_k'].dropna().unique())
sgg_sel = st.selectbox("시군구 선택", sgg_list)

row = gdf[gdf['sgg_nm_k'] == sgg_sel].iloc[0]

st.markdown(f"## {row['sido_nm_k']} {row['sgg_nm_k']}")
st.metric("생활인프라 편리성 종합지수", f"{row['infra_idx']:.1f}점",
          f"전국 {percentile_rank(gdf['infra_idx'], row['infra_idx'])}백분위")

st.markdown("---")

col_l, col_r = st.columns(2)

# --- 레이더 차트 (편리성 지수) ---
with col_l:
    st.subheader("부문별 편리성 레이더")
    labels = [s['name'] for s in SECTORS]
    values = [row[f'conv_{s["col"]}'] for s in SECTORS]
    nat_avg = [gdf[f'conv_{s["col"]}'].mean() for s in SECTORS]

    fig_radar = go.Figure()
    fig_radar.add_trace(go.Scatterpolar(
        r=values + [values[0]],
        theta=labels + [labels[0]],
        fill='toself',
        name=sgg_sel,
        line_color='royalblue',
    ))
    fig_radar.add_trace(go.Scatterpolar(
        r=nat_avg + [nat_avg[0]],
        theta=labels + [labels[0]],
        fill='toself',
        name='전국 평균',
        line_color='tomato',
        opacity=0.4,
    ))
    fig_radar.update_layout(
        polar=dict(radialaxis=dict(visible=True)),
        showlegend=True,
        height=400,
        margin=dict(l=40, r=40, t=40, b=40),
    )
    st.plotly_chart(fig_radar, use_container_width=True)

# --- 공급·향유·충족 3단 막대 ---
with col_r:
    st.subheader("부문별 공급·향유·충족 비교")
    rows = []
    for s in SECTORS:
        for prefix, label in [('sup', '공급수준'), ('pop', '향유수준'), ('acc', '충족수준')]:
            val = row[f'{prefix}_{s["col"]}']
            avg = gdf[f'{prefix}_{s["col"]}'].mean()
            rows.append({'부문': s['name'], '구분': label, '값': val, '전국평균': avg})
    df_bar = pd.DataFrame(rows)

    fig_bar = px.bar(
        df_bar,
        x='부문', y='값', color='구분',
        barmode='group',
        color_discrete_map={'공급수준': '#4C72B0', '향유수준': '#DD8452', '충족수준': '#55A868'},
        height=400,
        labels={'값': '지수값'},
        title=f'{sgg_sel} 공급·향유·충족 부문별 비교',
    )
    # 전국 평균선 (투명 scatter trick — 각 구분별)
    for prefix, label, color in [
        ('sup', '공급수준', '#4C72B0'),
        ('pop', '향유수준', '#DD8452'),
        ('acc', '충족수준', '#55A868'),
    ]:
        avgs = [gdf[f'{prefix}_{s["col"]}'].mean() for s in SECTORS]
        # 전국 평균 라인 표시 생략 (복잡해짐), 툴팁으로만
    fig_bar.update_layout(margin=dict(l=20, r=20, t=50, b=20))
    st.plotly_chart(fig_bar, use_container_width=True)

# --- 수치 테이블 ---
st.markdown("---")
st.subheader("상세 수치")

table_rows = []
for s in SECTORS:
    pct_conv = percentile_rank(gdf[f'conv_{s["col"]}'], row[f'conv_{s["col"]}'])
    table_rows.append({
        '부문': s['name'],
        '편리성 지수': round(row[f'conv_{s["col"]}'], 2),
        '편리성 백분위': f"{pct_conv}%ile",
        '공급수준': round(row[f'sup_{s["col"]}'], 3),
        '향유수준': round(row[f'pop_{s["col"]}'], 3),
        '충족수준': round(row[f'acc_{s["col"]}'], 3),
    })

st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
