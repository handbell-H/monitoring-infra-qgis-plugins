"""
공통 데이터 로더 — 세 SHP를 로드·병합해 캐시로 제공
"""
import os
import json
import geopandas as gpd
import pandas as pd
import streamlit as st

# 데이터 경로 (환경변수 DATA_DIR 또는 ../output)
DATA_DIR = os.environ.get(
    'DATA_DIR',
    os.path.join(os.path.dirname(__file__), '..', 'output')
)

SECTORS = [
    {'col': 'edu',  'name': '교육학습', 'label': '교육학습 편리성'},
    {'col': 'care', 'name': '돌봄복지', 'label': '돌봄복지 편리성'},
    {'col': 'med',  'name': '보건의료', 'label': '보건의료 편리성'},
    {'col': 'safe', 'name': '안전치안', 'label': '안전치안 편리성'},
    {'col': 'cult', 'name': '체육문화', 'label': '체육문화 편리성'},
]
SEC_COLS  = [s['col'] for s in SECTORS]
SEC_NAMES = [s['name'] for s in SECTORS]


@st.cache_data
def load_merged() -> gpd.GeoDataFrame:
    """세 SHP 로드 → 병합 → WGS84 변환 → 캐시 반환"""
    def _load(fname):
        path = os.path.join(DATA_DIR, fname)
        if not os.path.exists(path):
            return None
        return gpd.read_file(path).to_crs('EPSG:4326')

    sup = _load('supply_index.shp')
    pop = _load('service_pop_index.shp')
    acc = _load('access_index.shp')
    com = _load('composite_index.shp')

    if sup is None:
        st.error("supply_index.shp 를 찾을 수 없습니다.")
        st.stop()

    id_cols = [c for c in ['sgg_cd', 'sgg_nm_k', 'sido_nm_k', 'sido_cd'] if c in sup.columns]
    base = sup[id_cols + ['geometry']].copy()

    # 공급수준 부문 컬럼 → sup_*
    for s in SEC_COLS:
        col = f'{s}_avg'
        base[f'sup_{s}'] = sup[col] if col in sup.columns else float('nan')

    # 향유수준 부문 컬럼 → pop_*
    if pop is not None:
        pop_m = pop.drop(columns='geometry').set_index('sgg_cd')
        for s in SEC_COLS:
            col = f'{s}_avg'
            base[f'pop_{s}'] = base['sgg_cd'].map(pop_m[col]) if col in pop_m.columns else float('nan')
    else:
        for s in SEC_COLS:
            base[f'pop_{s}'] = float('nan')

    # 충족수준 부문 컬럼 → acc_*
    if acc is not None:
        acc_m = acc.drop(columns='geometry').set_index('sgg_cd')
        for s in SEC_COLS:
            col = f'{s}_std'
            base[f'acc_{s}'] = base['sgg_cd'].map(acc_m[col]) if col in acc_m.columns else float('nan')
    else:
        for s in SEC_COLS:
            base[f'acc_{s}'] = float('nan')

    # 종합지수 컬럼
    if com is not None:
        com_m = com.drop(columns='geometry').set_index('sgg_cd')
        for s in SEC_COLS:
            col = f'{s}_conv'
            base[f'conv_{s}'] = base['sgg_cd'].map(com_m[col]) if col in com_m.columns else float('nan')
        base['infra_idx'] = base['sgg_cd'].map(com_m['infra_idx']) if 'infra_idx' in com_m.columns else float('nan')
    else:
        for s in SEC_COLS:
            base[f'conv_{s}'] = float('nan')
        base['infra_idx'] = float('nan')

    return base


@st.cache_data
def get_geojson(_gdf: gpd.GeoDataFrame) -> dict:
    """GeoDataFrame → GeoJSON dict (plotly용)"""
    return json.loads(_gdf[['sgg_cd', 'geometry']].to_json())


def percentile_rank(series: pd.Series, value: float) -> float:
    """값의 전국 백분위 순위 (0~100)"""
    return round((series < value).sum() / series.notna().sum() * 100, 1)
