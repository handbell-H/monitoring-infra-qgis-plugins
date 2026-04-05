"""
Geopandas 기반 고속 처리
공간 연산: geopandas.sjoin (벡터 연산), 병렬: ThreadPoolExecutor
"""
import os
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import geopandas as gpd
import pandas as pd
from qgis.core import QgsVectorLayer


# ── 시설명 → 영문 컬럼 기반명 (≤6자, _cnt/_sup/_std 붙여도 ≤10자)
FACILITY_COL_MAP = {
    '국공립도서관':    'publib',
    '작은도서관':      'smlib',
    '도서관':          'lib',
    '어린이집':        'daycar',
    '유치원':          'kinder',
    '초등학교':        'elem',
    '온종일돌봄센터':  'allday',
    '온종일 돌봄센터': 'allday',
    '종합사회복지관':  'welfar',
    '노인여가복지시설':'snrlei',
    '노인복지관':      'snrwel',
    '경로당':          'snrctr',
    '종합병원':        'hosp',
    '보건기관':        'health',
    '의원':            'clinic',
    '약국':            'pharma',
    '지진옥외대피소':  'eqshlt',
    '응급의료기관':    'emrgin',
    '응급의료시설':    'emerg',
    '경찰서':          'police',
    '소방서':          'fire',
    '생활권공원':      'lfpark',
    '생활공원':        'lfpar2',
    '주제공원':        'thpark',
    '공연문화시설':    'cultur',
    '공공체육시설':    'sports',
    '버스정류장':      'busstp',
}

SECTOR_COL_MAP = {
    '교육학습': 'edu',
    '돌봄복지': 'care',
    '보건의료': 'med',
    '안전치안': 'safe',
    '체육문화': 'cult',
    '미분류':   'misc',
}

DEFAULT_SECTORS = {
    '교육학습': [
        '도서관', '어린이집', '유치원', '초등학교',
    ],
    '돌봄복지': [
        '온종일돌봄센터', '종합사회복지관', '노인여가복지시설', '경로당',
    ],
    '보건의료': [
        '종합병원', '보건기관', '의원', '약국',
    ],
    '안전치안': [
        '지진옥외대피소', '응급의료기관', '응급의료시설',
        '경찰서', '소방서',
    ],
    '체육문화': [
        '생활권공원', '생활공원', '주제공원',
        '공연문화시설', '공공체육시설',
    ],
}


def detect_sector(filename):
    stem = os.path.splitext(filename)[0].replace(' ', '')
    for sector, keywords in DEFAULT_SECTORS.items():
        for kw in keywords:
            if kw.replace(' ', '') in stem:
                return sector, kw
    return None, None


def _eng_col(display_name, fallback, used):
    stem = display_name.replace(' ', '')
    base = None
    for kor, eng in FACILITY_COL_MAP.items():
        if kor.replace(' ', '') in stem:
            base = eng
            break
    if base is None:
        base = fallback[:6]

    col = base
    suffix = 2
    while col in used:
        col = f'{base[:4]}{suffix}'
        suffix += 1
    used.add(col)
    return col


def load_shp_columns(shp_path):
    layer = QgsVectorLayer(shp_path, '__tmp__', 'ogr')
    if not layer.isValid():
        raise ValueError(f"SHP 로드 실패: {shp_path}")
    return [f.name() for f in layer.fields()]


# ── 로그 변환 (반사 변환 포함)
def _log_transform(s: pd.Series, method: str) -> pd.Series:
    if method == 'ln':
        return np.log1p(s)
    elif method == 'log10':
        return np.log10(s + 1)
    elif method == 'reflected_ln':
        mx = s.max()
        return np.log1p(mx - s)
    elif method == 'reflected_log10':
        mx = s.max()
        return np.log10(mx - s + 1)
    else:
        return s.copy()


# ── 표준화
def _standardize_series(s: pd.Series, method: str) -> pd.Series:
    if method == 'minmax':
        mn, mx = s.min(), s.max()
        if mx > mn:
            return (s - mn) / (mx - mn)
        return pd.Series(0.0, index=s.index)

    elif method == 'zscore':
        mean, sd = s.mean(), s.std(ddof=0)
        if sd > 0:
            return (s - mean) / sd
        return pd.Series(0.0, index=s.index)

    elif method == 'tscore':
        mean, sd = s.mean(), s.std(ddof=0)
        if sd > 0:
            return 50.0 + 10.0 * (s - mean) / sd
        return pd.Series(50.0, index=s.index)

    elif method == 'percentile':
        return s.rank(method='average') / len(s)

    else:  # none
        return s.copy()


# ── 단일 시설 공간조인 (스레드에서 실행)
def _count_facility(pf, fac_idx, total, sgg_crs, sgg_slim, sgg_col, log_fn):
    try:
        pt_gdf = gpd.read_file(pf['filepath'])
    except Exception as e:
        log_fn(f"    [경고] 파일 로드 실패: {pf['display_name']} — {e}")
        return fac_idx, pd.Series(dtype=int)

    if pt_gdf.empty:
        log_fn(f"  [{fac_idx+1}/{total}] {pf['sector']} / {pf['display_name']} → 0개")
        return fac_idx, pd.Series(dtype=int)

    if pt_gdf.crs != sgg_crs:
        pt_gdf = pt_gdf.to_crs(sgg_crs)

    joined = gpd.sjoin(
        pt_gdf[['geometry']],
        sgg_slim,
        how='left',
        predicate='within',
    )
    counts = joined.groupby(sgg_col).size()
    log_fn(f"  [{fac_idx+1}/{total}] {pf['sector']} / {pf['display_name']} → {counts.sum():,}개")
    return fac_idx, counts


# ── 1단계: 거주 km² 집계 + 시설별 공간조인
def compute_sup(point_files_info, sgg_shp, grid_shp, grid_pop_col,
                sgg_col, log_fn=print):
    # ── SGG SHP 로드
    log_fn("시군구 경계 SHP 로드 중...")
    sgg_gdf = gpd.read_file(sgg_shp)
    sgg_crs = sgg_gdf.crs
    log_fn(f"  → 시군구 {len(sgg_gdf)}개 로드 완료")

    # ── 격자 SHP → 거주 격자 필터 → centroid → 시군구 공간조인
    log_fn("\n[1단계] 거주지 1km² 면적 산출 (인구 격자 기반)")
    grid_gdf = gpd.read_file(grid_shp)
    grid_gdf[grid_pop_col] = pd.to_numeric(
        grid_gdf[grid_pop_col], errors='coerce'
    ).fillna(0)
    log_fn(f"  격자 전체: {len(grid_gdf):,}개")

    # ── 1. 격자 폴리곤 → centroid 포인트 (전체)
    grid_pts = gpd.GeoDataFrame(
        grid_gdf[[grid_pop_col]],
        geometry=grid_gdf.geometry.centroid,
        crs=grid_gdf.crs,
    )

    # ── 2. 인구 > 0 포인트만 필터
    grid_pts = grid_pts[grid_pts[grid_pop_col] > 0].copy()
    log_fn(f"  인구 > 0 격자(포인트): {len(grid_pts):,}개")

    # ── 3. 시군구 공간조인
    if grid_pts.crs != sgg_crs:
        grid_pts = grid_pts.to_crs(sgg_crs)

    joined_grid = gpd.sjoin(
        grid_pts,
        sgg_gdf[[sgg_col, 'geometry']],
        how='inner',
        predicate='within',
    )

    # ── 4. 시군구 cd 기준 그룹바이
    res_km2 = joined_grid.groupby(sgg_col).size()
    log_fn(f"  시군구별 거주 km² 산출 완료 (평균 {res_km2.mean():.1f} km²/시군구)")

    # ── 5. 시군구 경계에 붙이기
    result = sgg_gdf[[sgg_col]].set_index(sgg_col).copy()
    result['res_km2'] = result.index.map(res_km2).fillna(0).astype(int)

    # ── fac_meta 구성
    used_cols = set()
    fac_meta = []
    for i, pf in enumerate(point_files_info):
        col = _eng_col(pf['display_name'], f'f{i:02d}', used_cols)
        fac_meta.append({
            'idx': i, 'col': col,
            'sector': pf['sector'], 'name': pf['display_name'],
            'filepath': pf['filepath'],
        })

    # ── 2단계: 병렬 시설 공간조인
    log_fn(f"\n[2단계] 시설별 공간조인")
    n = len(point_files_info)
    max_workers = min(4, n)
    log_fn(f"  병렬 처리: {n}개 파일 / {max_workers} 스레드")

    sgg_slim = sgg_gdf[[sgg_col, 'geometry']].copy()
    count_results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _count_facility,
                pf, i, n, sgg_crs, sgg_slim, sgg_col, log_fn,
            ): i
            for i, pf in enumerate(point_files_info)
        }
        for future in as_completed(futures):
            fac_idx, counts = future.result()
            count_results[fac_idx] = counts

    # ── 개수 컬럼 + 거주 1km²당 시설 수
    for fm in fac_meta:
        cnt_series = count_results.get(fm['idx'], pd.Series(dtype=int))
        result[fm['col'] + '_cnt'] = result.index.map(cnt_series).fillna(0).astype(int)

    for fm in fac_meta:
        cnt_col = fm['col'] + '_cnt'
        sup_col = fm['col'] + '_sup'
        cnt = result[cnt_col].fillna(0)
        res = result['res_km2'].fillna(0)
        result[sup_col] = (cnt / res.replace(0, float('nan'))).fillna(0.0)
        s = result[sup_col]
        log_fn(f"  {fm['name']}: _sup 범위 [{s.min():.4f} ~ {s.max():.4f}], 평균 {s.mean():.4f}")

    return result, fac_meta


# ── 2단계: 로그 변환 + 표준화 + SHP 저장
def finalize_supply(sgg_df, fac_meta, sgg_shp, sgg_col, std_method,
                    output_dir, fac_log_transforms=None, log_fn=print):
    os.makedirs(output_dir, exist_ok=True)

    result = sgg_df.copy()
    _flx = fac_log_transforms or {}

    log_fn(f"\n[3단계] 시설별 로그 변환 + 표준화 ({std_method})")

    for fm in fac_meta:
        lt = _flx.get(fm['name'], 'none')
        fm['log_transform'] = lt
        sup_col = fm['col'] + '_sup'

        if lt != 'none':
            result[fm['col'] + '_log'] = _log_transform(result[sup_col], lt)
            src = result[fm['col'] + '_log']
        else:
            src = result[sup_col]

        std_val = _standardize_series(src, std_method)

        # 반사 변환 후 역전 보정
        if lt in ('reflected_ln', 'reflected_log10'):
            if std_method == 'minmax':
                std_val = 1 - std_val
            elif std_method == 'zscore':
                std_val = -std_val
            elif std_method == 'tscore':
                std_val = 100 - std_val
            elif std_method == 'percentile':
                std_val = 1 - std_val
            else:  # none
                mn, mx = std_val.min(), std_val.max()
                std_val = mx + mn - std_val

        result[fm['col'] + '_std'] = std_val
        log_fn(f"  {fm['name']}: 로그={lt}, 표준화={std_method} 완료")

    # ── 부문 평균
    log_fn("\n[4단계] 부문별 평균 산출")
    sectors_order = list(dict.fromkeys(fm['sector'] for fm in fac_meta))
    sec_meta = []
    for j, sec_name in enumerate(sectors_order):
        s_col = SECTOR_COL_MAP.get(sec_name, f's{j:02d}')
        sec_meta.append({'idx': j, 'col': s_col, 'name': sec_name})
        std_cols = [fm['col'] + '_std' for fm in fac_meta if fm['sector'] == sec_name]
        result[s_col + '_avg'] = result[std_cols].mean(axis=1)
        log_fn(f"  {sec_name}: {len(std_cols)}개 시설 평균")

    # ── 컬럼 순서 정렬
    cnt_cols = [fm['col'] + '_cnt' for fm in fac_meta]
    sup_cols = [fm['col'] + '_sup' for fm in fac_meta]
    log_cols = [fm['col'] + '_log' for fm in fac_meta
                if fm.get('log_transform', 'none') != 'none']
    std_cols = [fm['col'] + '_std' for fm in fac_meta]
    avg_cols = [sm['col'] + '_avg' for sm in sec_meta]

    ordered_cols = ['res_km2'] + cnt_cols + sup_cols + log_cols + std_cols + avg_cols
    ordered_cols = [c for c in ordered_cols if c in result.columns]
    result = result[ordered_cols].round(4).reset_index()

    # ── geometry 병합 후 SHP 저장
    log_fn("\n결과 SHP 생성 중...")
    sgg_gdf = gpd.read_file(sgg_shp)
    result_gdf = sgg_gdf[['geometry', sgg_col]].merge(result, on=sgg_col, how='left')
    result_gdf = gpd.GeoDataFrame(result_gdf, geometry='geometry', crs=sgg_gdf.crs)

    out_shp = os.path.join(output_dir, 'supply_index.shp')
    result_gdf.to_file(out_shp, encoding='utf-8')
    log_fn(f"저장: {out_shp}  ({len(result_gdf):,}행)")

    meta = {
        'sgg_col':    sgg_col,
        'std_method': std_method,
        'facilities': fac_meta,
        'sectors':    sec_meta,
    }
    meta_path = os.path.join(output_dir, 'supply_meta.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    log_fn(f"메타: {meta_path}")

    return out_shp, meta
