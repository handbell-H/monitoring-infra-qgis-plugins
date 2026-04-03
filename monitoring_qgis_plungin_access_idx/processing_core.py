"""
접근성 충족 지수 - geopandas 기반
격자별 접근성 거리 → 이진화 → 부문별 충족 격자 비율 → 시군구 집계 → 표준화
"""
import os
import re
import math
import json
from functools import reduce

import numpy as np
import geopandas as gpd
import pandas as pd


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
    '교육학습': ['도서관', '어린이집', '유치원', '초등학교'],
    '돌봄복지': ['온종일돌봄센터', '종합사회복지관', '노인여가복지시설', '경로당'],
    '보건의료': ['종합병원', '보건기관', '의원', '약국'],
    '안전치안': ['지진옥외대피소', '응급의료기관', '응급의료시설', '경찰서', '소방서'],
    '체육문화': ['생활권공원', '생활공원', '주제공원', '공연문화시설', '공공체육시설'],
}

# 거리 기준 1km 시설 목록 (이외 알려진 시설은 5km)
THRESHOLD_1KM = {
    '어린이집', '유치원', '초등학교', '경로당', '작은도서관',
    '온종일돌봄센터', '온종일 돌봄센터', '생활권공원', '의원', '약국', '지진옥외대피소',
}
# 목록에 없는 미인식 시설의 폴백 (사용자 확인 필요)
DEFAULT_THRESHOLD_FALLBACK = 1.0


def extract_facility_name(filename):
    """파일명에서 시설명 추출. 예: '13.2 어린이집(시군구격자) 접근성' → '어린이집'"""
    match = re.search(r'[\d.]+\s+(.+?)\(시군구격자\)', filename)
    return match.group(1).strip() if match else None


def detect_sector(filename):
    """파일명에서 부문 자동 감지."""
    name = extract_facility_name(filename) or ''
    name_ns = name.replace(' ', '')
    for sector, keywords in DEFAULT_SECTORS.items():
        for kw in keywords:
            if kw.replace(' ', '') in name_ns:
                return sector, kw
    return None, None


def get_default_threshold(display_name):
    """
    시설명으로 디폴트 거리 기준(km)과 인식 여부 반환.
    Returns: (threshold_km, is_recognized)
      - is_recognized=False → 목록에 없는 시설, 사용자 확인 필요
    """
    name_ns = display_name.replace(' ', '')
    # 1km 목록 확인
    for kor in THRESHOLD_1KM:
        if kor.replace(' ', '') in name_ns:
            return 1.0, True
    # FACILITY_COL_MAP에 있는 기타 알려진 시설 → 5km
    for kor in FACILITY_COL_MAP:
        if kor.replace(' ', '') in name_ns:
            return 5.0, True
    # 미인식 시설 → 1km 폴백, 확인 필요
    return DEFAULT_THRESHOLD_FALLBACK, False


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


def _log_transform(s: pd.Series, method: str) -> pd.Series:
    """
    method:
      'ln'              → ln(x+1)             [오른꼬리]
      'log10'           → log10(x+1)           [오른꼬리]
      'reflected_ln'    → ln(max+1-x)          [왼꼬리: 반사 후 로그]
      'reflected_log10' → log10(max+1-x)       [왼꼬리: 반사 후 로그]
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


def compute_rat(scan_results, sgg_col, log_fn=print):
    """
    1~4단계: SHP 읽기 → 이진화 → 충족격자 판정 → 시군구별 충족 격자 비율 산출
    반환: (sgg_df, fac_meta, sec_meta)
      sgg_df   : 시군구별 _rat 컬럼 포함 DataFrame
      fac_meta : 시설 메타 리스트
      sec_meta : 부문 메타 리스트
    """
    # ── 1. 영문 컬럼명 할당
    used_cols = set()
    fac_meta = []
    for i, pf in enumerate(scan_results):
        col = _eng_col(pf['display_name'], f'f{i:02d}', used_cols)
        fac_meta.append({
            'idx': i, 'col': col,
            'sector': pf['sector'],
            'name': pf['display_name'],
            'threshold': float(pf['threshold']),
            'filepath': pf['filepath'],
        })

    # ── 2. SHP 읽기 + gid 기준 병합
    log_fn(f"\n[1단계] SHP 읽기 및 병합 ({len(fac_meta)}개 파일)")
    base_geo = None
    fac_dfs = []

    for fm in fac_meta:
        try:
            gdf = gpd.read_file(fm['filepath'])
        except Exception as e:
            log_fn(f"  [경고] 로드 실패: {fm['name']} — {e}")
            fac_dfs.append(pd.DataFrame(columns=['gid', fm['col']]))
            continue

        if base_geo is None:
            meta_cols = [c for c in ['gid', 'sgg_cd', 'sgg_nm_k', 'sido_cd', 'sido_nm_k']
                         if c in gdf.columns]
            base_geo = gdf[meta_cols + ['geometry']].copy()

        fac_dfs.append(gdf[['gid', 'value']].rename(columns={'value': fm['col']}))
        log_fn(f"  {fm['name']} ({fm['col']}) 읽기 완료")

    if base_geo is None:
        raise RuntimeError("처리할 SHP가 없습니다.")

    log_fn("  병합 중 (격자 수가 많아 시간이 걸릴 수 있습니다)...")
    merged = reduce(
        lambda l, r: l.merge(r, on='gid', how='left'),
        [base_geo] + fac_dfs
    )
    log_fn(f"  병합 완료: {len(merged):,}개 격자")

    # ── 3. 이진화 (거리 ≤ 기준 → 1)
    log_fn("\n[2단계] 이진화")
    for fm in fac_meta:
        thr = fm['threshold']
        merged[fm['col'] + '_bin'] = (
            merged[fm['col']].notna() &
            (merged[fm['col']] >= 0) &
            (merged[fm['col']] <= thr)
        ).astype(int)
        log_fn(f"  {fm['name']}: ≤{thr}km → 이진화 완료")

    # ── 4. 부문별 합산 + 충족 격자 판정 (50% 이상 만족)
    log_fn("\n[3단계] 부문별 충족 격자 판정")
    sectors_order = list(dict.fromkeys(fm['sector'] for fm in fac_meta))
    sec_meta = []

    for j, sec_name in enumerate(sectors_order):
        s_col = SECTOR_COL_MAP.get(sec_name, f's{j:02d}')
        fac_in_sec = [fm for fm in fac_meta if fm['sector'] == sec_name]
        n_fac = len(fac_in_sec)
        half_thr = math.ceil(n_fac / 2)

        bin_cols = [fm['col'] + '_bin' for fm in fac_in_sec]
        merged[s_col + '_sum'] = merged[bin_cols].sum(axis=1)
        merged[s_col + '_ok'] = (merged[s_col + '_sum'] >= half_thr).astype(int)

        sec_meta.append({
            'col': s_col, 'name': sec_name,
            'n_fac': n_fac, 'half_thr': half_thr,
        })
        log_fn(f"  {sec_name}: {n_fac}개 시설, 충족 기준 ≥{half_thr}개 ({half_thr}/{n_fac})")

    # ── 5. 시군구별 집계
    log_fn("\n[4단계] 시군구별 충족 격자 비율 산출")
    agg_col = sgg_col if sgg_col in merged.columns else 'sgg_cd'
    grouped = merged.groupby(agg_col)

    sgg_total = grouped['gid'].count().rename('total')
    sgg_df = sgg_total.to_frame()

    for sm in sec_meta:
        ok_count = grouped[sm['col'] + '_ok'].sum()
        sgg_df[sm['col'] + '_ok'] = ok_count.astype(int)
        sgg_df[sm['col'] + '_rat'] = (ok_count / sgg_total).round(4)

    log_fn("충족 격자 비율 산출 완료.")
    return sgg_df, fac_meta, sec_meta


def finalize_pipeline(sgg_df, fac_meta, sec_meta, sgg_shp, sgg_col,
                      std_method, output_dir, sector_log_transforms=None, log_fn=print):
    """
    5~7단계: 로그 변환 → 표준화 → SHP 저장
    sgg_df, fac_meta, sec_meta: compute_rat() 반환값
    """
    os.makedirs(output_dir, exist_ok=True)

    # ── 6. 부문별 분포 통계 출력 → 로그 변환 → 표준화
    _slx = sector_log_transforms or {}
    _rec_labels = {
        'none': '변환 없음', 'ln': '자연로그',
        'log10': '상용로그', 'reflected_ln': '반사 자연로그', 'reflected_log10': '반사 상용로그',
    }

    log_fn(f"\n[5단계] 부문별 로그 변환 / 표준화 ({std_method})")
    log_fn("[분포 통계]  부문명          N    평균    최대    왜도")

    for sm in sec_meta:
        s = sgg_df[sm['col'] + '_rat'].fillna(0.0)
        skew = float(s.skew())
        if skew > 2.0:    rec = 'log10'
        elif skew > 1.0:  rec = 'ln'
        elif skew < -2.0: rec = 'reflected_log10'
        elif skew < -1.0: rec = 'reflected_ln'
        else:             rec = 'none'

        lt = _slx.get(sm['name'], 'none')
        note = '' if lt == rec else f'  (권장: {_rec_labels[rec]})'
        flag = '  ▶로그 권장' if skew > 1.0 else ('  ▶반사로그 권장' if skew < -1.0 else '')
        log_fn(
            f"  {sm['name']:<10}  "
            f"{len(s):3d}  {float(s.mean()):6.3f}  {float(s.max()):6.3f}  {skew:+.2f}{flag}"
        )
        sm['log_transform'] = lt

        # 로그 변환
        if lt != 'none':
            sgg_df[sm['col'] + '_log'] = _log_transform(s, lt)
            src_col = sm['col'] + '_log'
            log_fn(f"  {sm['name']}: 로그 변환 ({lt}){note}")
        else:
            src_col = sm['col'] + '_rat'
            if note:
                log_fn(f"  {sm['name']}: 변환 없음{note}")

        # 표준화
        sgg_df[sm['col'] + '_std'] = _standardize_series(
            sgg_df[src_col].fillna(0.0), std_method
        ).round(4)

        # 반사변환 시 방향 역전 복원
        if lt in ('reflected_ln', 'reflected_log10'):
            col = sm['col'] + '_std'
            if std_method == 'minmax':
                sgg_df[col] = 1.0 - sgg_df[col]
            elif std_method == 'zscore':
                sgg_df[col] = -sgg_df[col]
            elif std_method == 'tscore':
                sgg_df[col] = 100.0 - sgg_df[col]
            elif std_method == 'percentile':
                sgg_df[col] = 1.0 - sgg_df[col]
            elif std_method == 'none':
                mn, mx = sgg_df[col].min(), sgg_df[col].max()
                sgg_df[col] = mx + mn - sgg_df[col]
            log_fn(f"  {sm['name']}: 방향 역전 복원 완료")

        log_fn(f"  {sm['name']}: 표준화 완료")

    sgg_df = sgg_df.reset_index()

    # ── 7. 시군구 경계와 병합 후 SHP 저장
    log_fn("\n결과 SHP 생성 중...")
    sgg_gdf = gpd.read_file(sgg_shp)
    result = sgg_gdf.merge(sgg_df, on=sgg_col, how='left')
    result = gpd.GeoDataFrame(result, geometry='geometry', crs=sgg_gdf.crs)

    out_shp = os.path.join(output_dir, 'access_index.shp')
    result.to_file(out_shp, encoding='utf-8')
    log_fn(f"저장: {out_shp}  ({len(result):,}행)")

    meta = {
        'sgg_col': sgg_col,
        'std_method': std_method,
        'facilities': fac_meta,
        'sectors': sec_meta,
    }
    with open(os.path.join(output_dir, 'access_meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return out_shp, meta
