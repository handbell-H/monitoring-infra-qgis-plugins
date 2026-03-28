"""
생활인프라 편리성 모니터링 대시보드 — 홈
"""
import streamlit as st
from data_loader import load_merged, SECTORS

st.set_page_config(
    page_title="생활인프라 편리성 모니터링",
    page_icon="🏙️",
    layout="wide",
)

st.title("생활인프라 편리성 모니터링 대시보드")
st.caption("제작: 손종혁 (국토연구원 국토모니터링연구센터)")
st.markdown("---")

gdf = load_merged()

# 전국 요약 지표
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("분석 시군구 수", f"{len(gdf):,}개")
with col2:
    v = gdf['infra_idx'].mean()
    st.metric("전국 평균 종합지수", f"{v:.1f}점")
with col3:
    v = gdf['infra_idx'].max()
    nm = gdf.loc[gdf['infra_idx'].idxmax(), 'sgg_nm_k']
    st.metric("최고 지수", f"{v:.1f}점", nm)
with col4:
    v = gdf['infra_idx'].min()
    nm = gdf.loc[gdf['infra_idx'].idxmin(), 'sgg_nm_k']
    st.metric("최저 지수", f"{v:.1f}점", nm)

st.markdown("---")

# 부문별 전국 평균
st.subheader("부문별 전국 평균 (편리성 지수)")
cols = st.columns(len(SECTORS))
for c, s in zip(cols, SECTORS):
    val = gdf[f'conv_{s["col"]}'].mean()
    c.metric(s['name'], f"{val:.1f}")

st.markdown("---")

st.markdown("""
### 페이지 안내

| 페이지 | 내용 |
|--------|------|
| **지도 뷰** | 시군구 단위 코로플레스 지도 — 지수·부문 선택 |
| **시군구 상세** | 선택 시군구의 레이더 차트 + 부문별 막대 |
| **순위 비교** | 전국 순위 테이블 + 두 시군구 비교 |
| **분포 분석** | 히스토그램 · 박스플롯 · 산점도 |

### 분석 체계

```
공급수준 ─┐
향유수준 ─┼─ 부문별 가중 합산 ─► 교육학습 편리성
충족수준 ─┘                      돌봄복지 편리성
                                 보건의료 편리성  ─► 생활인프라
                                 안전치안 편리성       편리성
                                 체육문화 편리성    종합지수(0~100)
```
""")
