"""
서비스권역 내 인구비율 분석 - geopandas 기반
각 SHP의 value_r 컬럼을 읽어 부문별 표준화·평균 산출
공간조인 불필요 (값이 이미 시군구 단위로 집계됨)
"""
import os
import re
from functools import reduce

import numpy as np
import geopandas as gpd
import pandas as pd
from qgis.core import QgsVectorLayer


# ── 시설명 → 영문 컬럼 기반명 (≤6자)
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


def extract_facility_name(filename):
    """파일명에서 시설명 추출. 예: '112.4 보건기관(시군구) ...' → '보건기관'"""
    match = re.search(r'[\d.]+\s+(.+?)\(시군구\)', filename)
    return match.group(1).strip() if match else None


def detect_sector(filename):
    """파일명에서 부문 자동 감지."""
    name = extract_facility_name(filename) or ''
    name_nospace = name.replace(' ', '')
    for sector, keywords in DEFAULT_SECTORS.items():
        for kw in keywords:
            if kw.replace(' ', '') in name_nospace:
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


def scan_stats(filepath, value_col='value_r'):
    """SHP 파일에서 value_col 기초 통계 반환. 실패 시 None."""
    try:
        gdf = gpd.read_file(filepath)
        if value_col not in gdf.columns:
            return None
        s = pd.to_numeric(gdf[value_col], errors='coerce').dropna()
        if s.empty:
            return None
        return {
            'n':    len(s),
            'mean': float(s.mean()),
            'max':  float(s.max()),
            'skew': float(s.skew()),
        }
    except Exception:
        return None


def recommend_log_transform(stats_list):
    """
    시설별 통계 목록으로 로그 변환 권장 방법 반환.
      평균 왜도 > 2.0   → 'log10'
      평균 왜도 > 1.0   → 'ln'
      평균 왜도 < -2.0  → 'reflected_log10'
      평균 왜도 < -1.0  → 'reflected_ln'
      그 외             → 'none'
    """
    skews = [st['skew'] for st in stats_list if st is not None]
    if not skews:
        return 'none'
    mean_skew = sum(skews) / len(skews)
    if mean_skew > 2.0:
        return 'log10'
    elif mean_skew > 1.0:
        return 'ln'
    elif mean_skew < -2.0:
        return 'reflected_log10'
    elif mean_skew < -1.0:
        return 'reflected_ln'
    else:
        return 'none'


def _log_transform(s: pd.Series, method: str) -> pd.Series:
    """
    method:
      'ln'              → ln(x+1)             [오른꼬리]
      'log10'           → log10(x+1)           [오른꼬리]
      'reflected_ln'    → ln(max+1-x)          [왼꼬리: 반사 후 로그]
      'reflected_log10' → log10(max+1-x)       [왼꼬리: 반사 후 로그]
      'none'            → 원값
    반사변환 시 반사값 최솟값 = 1이므로 +1 불필요.
    """
    if method == 'ln':
        return np.log1p(s)
    elif method == 'log10':
        return np.log10(s + 1)
    elif method == 'reflected_ln':
        return np.log(s.max() + 1 - s)
    elif method == 'reflected_log10':
        return np.log10(s.max() + 1 - s)
    else:
        return s.copy()


def _standardize_series(s, method):
    if method == 'minmax':
        mn, mx = s.min(), s.max()
        return (s - mn) / (mx - mn) if mx > mn else pd.Series(0.0, index=s.index)
    elif method == 'zscore':
        mean, sd = s.mean(), s.std(ddof=0)
        return (s - mean) / sd if sd > 0 else pd.Series(0.0, index=s.index)
    elif method == 'tscore':
        mean, sd = s.mean(), s.std(ddof=0)
        return 50.0 + 10.0 * (s - mean) / sd if sd > 0 else pd.Series(50.0, index=s.index)
    elif method == 'percentile':
        return s.rank(method='average') / len(s)
    else:
        return s.copy()


def run_pipeline(scan_results, sgg_col, std_method, output_dir, log_fn=print):
    """
    scan_results : list of {filepath, sector, display_name}
    sgg_col      : 시군구 식별 컬럼명
    std_method   : 'minmax' | 'zscore' | 'percentile' | 'none'
    output_dir   : 출력 폴더
    """
    os.makedirs(output_dir, exist_ok=True)

    # ── 1. 영문 컬럼명 할당
    used_cols = set()
    fac_meta = []
    for i, pf in enumerate(scan_results):
        col = _eng_col(pf['display_name'], f'f{i:02d}', used_cols)
        fac_meta.append({
            'idx': i, 'col': col,
            'sector': pf['sector'],
            'name': pf['display_name'],
            'filepath': pf['filepath'],
            'log_transform': pf.get('log_transform', 'none'),
        })

    # ── 2. SHP 읽기 + reduce로 병합
    log_fn(f"\n[1단계] SHP 읽기 및 병합 ({len(fac_meta)}개)")

    base_geo = None
    fac_dfs  = []

    for fm in fac_meta:
        try:
            gdf = gpd.read_file(fm['filepath'])
        except Exception as e:
            log_fn(f"  [경고] 로드 실패: {fm['name']} — {e}")
            fac_dfs.append(pd.DataFrame(columns=[sgg_col, fm['col']]))
            continue

        if base_geo is None:
            meta_cols = [c for c in [sgg_col, 'sgg_nm_k', 'sido_cd', 'sido_nm_k']
                         if c in gdf.columns and c != sgg_col]
            base_geo = gdf[[sgg_col] + meta_cols + ['geometry']].copy()

        val_df = gdf[[sgg_col, 'value_r']].rename(columns={'value_r': fm['col']})
        fac_dfs.append(val_df)
        log_fn(f"  {fm['name']} ({fm['col']}) 읽기 완료")

    if base_geo is None:
        raise RuntimeError("처리할 SHP가 없습니다.")

    merged = reduce(
        lambda l, r: l.merge(r, on=sgg_col, how='left'),
        [base_geo] + fac_dfs
    )

    # ── 3. 시설별 로그 변환 → 표준화
    log_fn(f"\n[2단계] 시설별 로그 변환 / 표준화 ({std_method})")
    val_cols = [fm['col'] for fm in fac_meta]

    for fm in fac_meta:
        lt = fm['log_transform']

        if lt != 'none':
            merged[fm['col'] + '_log'] = _log_transform(
                merged[fm['col']].fillna(0.0), lt
            )
            src_col = fm['col'] + '_log'
            log_fn(f"  {fm['name']}: 로그 변환 ({lt})")
        else:
            src_col = fm['col']

        merged[fm['col'] + '_std'] = _standardize_series(
            merged[src_col].fillna(0.0), std_method
        )

        # 반사변환 시 방향 역전 복원
        if lt in ('reflected_ln', 'reflected_log10'):
            col = fm['col'] + '_std'
            if std_method == 'minmax':
                merged[col] = 1.0 - merged[col]
            elif std_method == 'zscore':
                merged[col] = -merged[col]
            elif std_method == 'tscore':
                merged[col] = 100.0 - merged[col]
            elif std_method == 'percentile':
                merged[col] = 1.0 - merged[col]
            elif std_method == 'none':
                mn, mx = merged[col].min(), merged[col].max()
                merged[col] = mx + mn - merged[col]

        log_fn(f"  {fm['name']}: 표준화 완료")

    std_cols = [fm['col'] + '_std' for fm in fac_meta]

    # ── 4. 부문 평균
    log_fn("\n[3단계] 부문별 평균 산출")
    sectors_order = list(dict.fromkeys(fm['sector'] for fm in fac_meta))
    sec_meta = []
    avg_cols = []

    for j, sec_name in enumerate(sectors_order):
        s_col = SECTOR_COL_MAP.get(sec_name, f's{j:02d}')
        sec_meta.append({'col': s_col, 'name': sec_name})
        fac_std = [fm['col'] + '_std' for fm in fac_meta if fm['sector'] == sec_name]
        merged[s_col + '_avg'] = merged[fac_std].mean(axis=1)
        avg_cols.append(s_col + '_avg')
        log_fn(f"  {sec_name}: {len(fac_std)}개 시설 평균")

    # ── 5. 컬럼 순서 정렬: 원값 → [로그] → 표준화 → 부문평균
    log_cols = [fm['col'] + '_log' for fm in fac_meta if fm['log_transform'] != 'none']
    meta_cols = [c for c in [sgg_col, 'sgg_nm_k', 'sido_cd', 'sido_nm_k']
                 if c in merged.columns]
    ordered = meta_cols + val_cols + log_cols + std_cols + avg_cols + ['geometry']
    result = gpd.GeoDataFrame(
        merged[[c for c in ordered if c in merged.columns]],
        geometry='geometry', crs=base_geo.crs
    )

    # ── 6. SHP 저장
    log_fn("\n결과 SHP 생성 중...")
    out_shp = os.path.join(output_dir, 'service_pop_index.shp')
    result.to_file(out_shp, encoding='utf-8')
    log_fn(f"저장: {out_shp}  ({len(result):,}행)")

    import json
    meta = {'sgg_col': sgg_col, 'std_method': std_method,
            'facilities': fac_meta, 'sectors': sec_meta}
    with open(os.path.join(output_dir, 'service_pop_meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return out_shp, meta
