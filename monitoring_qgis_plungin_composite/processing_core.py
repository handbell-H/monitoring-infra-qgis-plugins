"""
생활인프라 편리성 종합지수
공급수준(supply) · 향유수준(service_pop) · 충족수준(access) 결과 SHP를
부문별 가중 합산 → 부문 편리성 → 종합지수 (0~100 rescaling)
"""
import os
import json

import geopandas as gpd
import pandas as pd


# ── 5개 부문 정의 ────────────────────────────────────────────
SECTORS = [
    {'col': 'edu',  'name': '교육학습', 'label': '교육학습 편리성', 'out': 'edu_conv'},
    {'col': 'care', 'name': '돌봄복지', 'label': '돌봄복지 편리성', 'out': 'care_conv'},
    {'col': 'med',  'name': '보건의료', 'label': '보건의료 편리성', 'out': 'med_conv'},
    {'col': 'safe', 'name': '안전치안', 'label': '안전치안 편리성', 'out': 'safe_conv'},
    {'col': 'cult', 'name': '체육문화', 'label': '체육문화 편리성', 'out': 'cult_conv'},
]

# 각 SHP에서 읽을 컬럼 패턴
def supply_col(sec):     return f'{sec}_avg'
def service_pop_col(sec): return f'{sec}_avg'
def access_col(sec):     return f'{sec}_std'


def run_pipeline(supply_shp, service_pop_shp, access_shp,
                 sgg_col,
                 input_weights,   # {sec_col: {'sup': w, 'pop': w, 'acc': w}}
                 sector_weights,  # {sec_col: w}
                 output_dir, log_fn=print):
    """
    input_weights  : 부문별 3개 입력 가중치
    sector_weights : 부문별 최종 합산 가중치 (합계 1.0)
    """
    os.makedirs(output_dir, exist_ok=True)

    # ── 1. 세 SHP 로드
    log_fn("SHP 로드 중...")
    sup_gdf = gpd.read_file(supply_shp)
    pop_gdf = gpd.read_file(service_pop_shp)
    acc_gdf = gpd.read_file(access_shp)
    log_fn(f"  공급수준: {len(sup_gdf)}행 / 향유수준: {len(pop_gdf)}행 / 충족수준: {len(acc_gdf)}행")

    # ── 2. sgg_col 기준 병합 (geometry는 공급수준 SHP 기준)
    log_fn(f"\n[1단계] '{sgg_col}' 기준 병합")
    needed_sup = [sgg_col] + [supply_col(s['col'])      for s in SECTORS if supply_col(s['col'])      in sup_gdf.columns]
    needed_pop = [sgg_col] + [service_pop_col(s['col']) for s in SECTORS if service_pop_col(s['col']) in pop_gdf.columns]
    needed_acc = [sgg_col] + [access_col(s['col'])      for s in SECTORS if access_col(s['col'])      in acc_gdf.columns]

    # 식별 컬럼 보존 (공급수준 기준)
    id_cols = [c for c in ['sgg_cd', 'sgg_nm_k', 'sgg_nm_e', 'sido_cd', 'sido_nm_k', 'sido_nm_e']
               if c in sup_gdf.columns]
    base = sup_gdf[id_cols + ['geometry']].copy()

    merged = (base
              .merge(sup_gdf[needed_sup],  on=sgg_col, how='left', suffixes=('', '_sup'))
              .merge(pop_gdf[needed_pop],  on=sgg_col, how='left', suffixes=('', '_pop'))
              .merge(acc_gdf[needed_acc],  on=sgg_col, how='left', suffixes=('', '_acc')))

    log_fn(f"  병합 완료: {len(merged)}행")

    # ── 3. 부문별 편리성 산출
    log_fn("\n[2단계] 부문별 편리성 산출")
    for sec in SECTORS:
        c    = sec['col']
        wts  = input_weights[c]
        w_s  = wts['sup']
        w_p  = wts['pop']
        w_a  = wts['acc']

        col_s = supply_col(c)
        col_p = service_pop_col(c)
        col_a = access_col(c)

        # suffix 처리 (merge 후 컬럼명 확인)
        def get_col(df, name, suffixes=('', '_sup', '_pop', '_acc')):
            for suf in suffixes:
                candidate = name + suf if suf else name
                if candidate in df.columns:
                    return candidate
            return None

        s_col = get_col(merged, col_s) or col_s
        p_col = get_col(merged, col_p + '_pop') or col_p
        a_col = get_col(merged, col_a) or col_a

        # 실제 존재하는 컬럼만 사용 (없으면 0)
        s_val = merged[s_col].fillna(0.0) if s_col in merged.columns else pd.Series(0.0, index=merged.index)
        p_val = merged[p_col].fillna(0.0) if p_col in merged.columns else pd.Series(0.0, index=merged.index)
        a_val = merged[a_col].fillna(0.0) if a_col in merged.columns else pd.Series(0.0, index=merged.index)

        merged[sec['out']] = (w_s * s_val + w_p * p_val + w_a * a_val).round(4)
        log_fn(f"  {sec['label']}: 공급 {w_s} + 향유 {w_p} + 충족 {w_a}")

    # ── 4. 종합지수 (가중 합산)
    log_fn("\n[3단계] 생활인프라 편리성 종합지수 산출")
    infra_raw = sum(
        sector_weights[s['col']] * merged[s['out']]
        for s in SECTORS
    )
    merged['infra_raw'] = infra_raw.round(4)
    log_fn(f"  부문 가중치: { {s['col']: sector_weights[s['col']] for s in SECTORS} }")

    # ── 5. 0~100 rescaling
    mn, mx = merged['infra_raw'].min(), merged['infra_raw'].max()
    if mx > mn:
        merged['infra_idx'] = ((merged['infra_raw'] - mn) / (mx - mn) * 100).round(2)
    else:
        merged['infra_idx'] = 50.0
    log_fn(f"  원값 범위: {mn:.4f} ~ {mx:.4f}  →  0 ~ 100 rescaling 완료")

    # ── 6. 결과 저장
    log_fn("\n결과 SHP 생성 중...")
    out_cols = id_cols + [s['out'] for s in SECTORS] + ['infra_raw', 'infra_idx', 'geometry']
    result = gpd.GeoDataFrame(
        merged[[c for c in out_cols if c in merged.columns]],
        geometry='geometry', crs=sup_gdf.crs
    )

    out_shp = os.path.join(output_dir, 'composite_index.shp')
    result.to_file(out_shp, encoding='utf-8')
    log_fn(f"저장: {out_shp}  ({len(result):,}행)")

    meta = {
        'sgg_col': sgg_col,
        'input_weights': input_weights,
        'sector_weights': sector_weights,
        'rescale_min': mn, 'rescale_max': mx,
    }
    with open(os.path.join(output_dir, 'composite_meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return out_shp, meta
