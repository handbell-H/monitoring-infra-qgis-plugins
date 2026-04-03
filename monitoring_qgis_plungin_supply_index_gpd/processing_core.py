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

# ── 디폴트 부문 매핑
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


# ── 로그 변환
def _log_transform(s: pd.Series, method: str) -> pd.Series:
    """method: 'ln' → ln(x+1),  'log10' → log10(x+1),  'none' → 원값"""
    if method == 'ln':
        return np.log1p(s)
    elif method == 'log10':
        return np.log10(s + 1)
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
def _count_facility(pf, fac_idx, total, pop_crs, pop_sgg, sgg_col, log_fn):
    """
    geopandas sjoin으로 포인트 → 시군구 카운팅.
    pop_sgg: pop GeoDataFrame의 [sgg_col, geometry] 슬라이스 (읽기 전용 공유)
    반환: (fac_idx, Series: sgg_key -> count)
    """
    try:
        pt_gdf = gpd.read_file(pf['filepath'])
    except Exception as e:
        log_fn(f"    [경고] 파일 로드 실패: {pf['display_name']} — {e}")
        return fac_idx, pd.Series(dtype=int)

    if pt_gdf.empty:
        log_fn(f"  [{fac_idx+1}/{total}] {pf['sector']} / {pf['display_name']} → 0개")
        return fac_idx, pd.Series(dtype=int)

    # CRS 통일
    if pt_gdf.crs != pop_crs:
        pt_gdf = pt_gdf.to_crs(pop_crs)

    # sjoin: within (포인트가 폴리곤 안에 있는지)
    joined = gpd.sjoin(
        pt_gdf[['geometry']],
        pop_sgg,
        how='left',
        predicate='within',
    )
    counts = joined.groupby(sgg_col).size()
    log_fn(f"  [{fac_idx+1}/{total}] {pf['sector']} / {pf['display_name']} → {counts.sum():,}개")
    return fac_idx, counts


# ── 메인 파이프라인
def run_pipeline(point_files_info, pop_shp, sgg_col, pop_col,
                 std_method, output_dir, log_transform='none', log_fn=print):
    os.makedirs(output_dir, exist_ok=True)

    # ── 1. 인구/경계 레이어 로드
    log_fn("인구(시군구 경계) SHP 로드 중...")
    pop_gdf = gpd.read_file(pop_shp)
    pop_gdf[pop_col] = pd.to_numeric(pop_gdf[pop_col], errors='coerce').fillna(0.0)
    pop_crs = pop_gdf.crs
    log_fn(f"  → 시군구 {len(pop_gdf)}개 로드 완료")

    # sjoin용 슬라이스 (읽기 전용 공유)
    pop_sgg = pop_gdf[[sgg_col, 'geometry']].copy()

    # ── 2. fac_meta 구성
    used_cols = set()
    fac_meta = []
    for i, pf in enumerate(point_files_info):
        col = _eng_col(pf['display_name'], f'f{i:02d}', used_cols)
        fac_meta.append({
            'idx': i, 'col': col,
            'sector': pf['sector'], 'name': pf['display_name'],
            'filepath': pf['filepath'],
        })

    # ── 3. 병렬 공간조인
    log_fn(f"\n[1단계] 시설별 1천인당 시설 수 산출")
    n = len(point_files_info)
    max_workers = min(4, n)
    log_fn(f"  병렬 처리: {n}개 파일 / {max_workers} 스레드")

    count_results = {}  # fac_idx → Series(sgg_key → count)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _count_facility,
                pf, i, n, pop_crs, pop_sgg, sgg_col, log_fn,
            ): i
            for i, pf in enumerate(point_files_info)
        }
        for future in as_completed(futures):
            fac_idx, counts = future.result()
            count_results[fac_idx] = counts

    # ── 4. 결과 DataFrame 구성 (인덱스: sgg_col, 인구 SHP 전체 컬럼 보존)
    result = pop_gdf.drop(columns=['geometry']).set_index(sgg_col).copy()

    # 개수 컬럼
    for fm in fac_meta:
        cnt_series = count_results.get(fm['idx'], pd.Series(dtype=int))
        result[fm['col'] + '_cnt'] = result.index.map(cnt_series).fillna(0).astype(int)

    # ── 5. 1천인당 시설 수 → 로그 변환 → 표준화
    log_fn(f"\n[2단계] 로그 변환 ({log_transform})  /  표준화 ({std_method})")

    # 공급수준 (1천인당)
    for fm in fac_meta:
        cnt_col = fm['col'] + '_cnt'
        sup_col = fm['col'] + '_sup'
        result[sup_col] = (
            result[cnt_col] / result[pop_col].replace(0, float('nan')) * 1000.0
        ).fillna(0.0)

    # 로그 변환 (none이 아닐 때만 _log 컬럼 생성)
    if log_transform != 'none':
        for fm in fac_meta:
            result[fm['col'] + '_log'] = _log_transform(
                result[fm['col'] + '_sup'], log_transform
            )
            log_fn(f"  {fm['name']}: 로그 변환 완료")

    # 표준화 (로그 변환값 우선, 없으면 원값)
    for fm in fac_meta:
        src_col = (fm['col'] + '_log') if log_transform != 'none' else (fm['col'] + '_sup')
        result[fm['col'] + '_std'] = _standardize_series(result[src_col], std_method)
        log_fn(f"  {fm['name']}: 표준화 완료")

    # ── 6. 부문 평균
    log_fn("\n[3단계] 부문별 평균 산출")
    sectors_order = list(dict.fromkeys(fm['sector'] for fm in fac_meta))
    sec_meta = []
    for j, sec_name in enumerate(sectors_order):
        s_col = SECTOR_COL_MAP.get(sec_name, f's{j:02d}')
        sec_meta.append({'idx': j, 'col': s_col, 'name': sec_name})
        std_cols = [fm['col'] + '_std' for fm in fac_meta if fm['sector'] == sec_name]
        result[s_col + '_avg'] = result[std_cols].mean(axis=1)
        log_fn(f"  {sec_name}: {len(std_cols)}개 시설 평균")

    # ── 7. 컬럼 순서 정렬: 인구SHP 원본 컬럼 → 개수 → 1천인당 → [로그] → 표준화 → 부문평균
    cnt_cols = [fm['col'] + '_cnt' for fm in fac_meta]
    sup_cols = [fm['col'] + '_sup' for fm in fac_meta]
    log_cols = [fm['col'] + '_log' for fm in fac_meta] if log_transform != 'none' else []
    std_cols = [fm['col'] + '_std' for fm in fac_meta]
    avg_cols = [sm['col'] + '_avg' for sm in sec_meta]

    extra_pop_cols = [c for c in pop_gdf.columns if c not in ('geometry', sgg_col)]
    ordered_cols = extra_pop_cols + cnt_cols + sup_cols + log_cols + std_cols + avg_cols
    result = result[ordered_cols].round(4).reset_index()

    # ── 8. geometry 병합 후 SHP 저장
    log_fn("\n결과 SHP 생성 중...")
    result_gdf = pop_gdf[['geometry', sgg_col]].merge(result, on=sgg_col, how='left')
    result_gdf = gpd.GeoDataFrame(result_gdf, geometry='geometry', crs=pop_crs)

    out_shp = os.path.join(output_dir, 'supply_index.shp')
    result_gdf.to_file(out_shp, encoding='utf-8')
    log_fn(f"저장: {out_shp}  ({len(result_gdf):,}행)")

    # 메타 JSON
    meta = {
        'sgg_col': sgg_col,
        'pop_col': pop_col,
        'log_transform': log_transform,
        'std_method': std_method,
        'facilities': fac_meta,
        'sectors': sec_meta,
    }
    meta_path = os.path.join(output_dir, 'supply_meta.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    log_fn(f"메타: {meta_path}")

    return out_shp, meta
