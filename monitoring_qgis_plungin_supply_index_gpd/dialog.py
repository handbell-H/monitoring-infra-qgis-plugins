import os
import json

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QPushButton, QFileDialog, QTextEdit,
    QComboBox, QGroupBox, QMessageBox, QProgressDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QInputDialog,
)
from qgis.PyQt.QtCore import QThread, pyqtSignal, Qt
from qgis.PyQt.QtGui import QFont, QColor
from qgis.core import QgsProject, QgsVectorLayer

from .processing_core import (
    DEFAULT_SECTORS, detect_sector, load_shp_columns,
    compute_sup, finalize_supply,
)


# ── 1단계 백그라운드: 거주 km² 집계 + 시설 공간조인
class SupWorker(QThread):
    log      = pyqtSignal(str)
    finished = pyqtSignal(object)   # (sgg_df, fac_meta)
    error    = pyqtSignal(str)

    def __init__(self, point_files_info, sgg_shp, grid_shp,
                 grid_pop_col, sgg_col):
        super().__init__()
        self.point_files_info = point_files_info
        self.sgg_shp          = sgg_shp
        self.grid_shp         = grid_shp
        self.grid_pop_col     = grid_pop_col
        self.sgg_col          = sgg_col

    def run(self):
        try:
            sgg_df, fac_meta = compute_sup(
                self.point_files_info, self.sgg_shp, self.grid_shp,
                self.grid_pop_col, self.sgg_col, log_fn=self.log.emit,
            )
            self.finished.emit((sgg_df, fac_meta))
        except Exception:
            import traceback
            self.error.emit(traceback.format_exc())


# ── 2단계 백그라운드: 로그 변환 + 표준화 + SHP 저장
class FinalWorker(QThread):
    log      = pyqtSignal(str)
    finished = pyqtSignal(str)   # out_shp path
    error    = pyqtSignal(str)

    def __init__(self, sgg_df, fac_meta, sgg_shp, sgg_col,
                 std_method, output_dir, fac_log_transforms):
        super().__init__()
        self.sgg_df             = sgg_df
        self.fac_meta           = fac_meta
        self.sgg_shp            = sgg_shp
        self.sgg_col            = sgg_col
        self.std_method         = std_method
        self.output_dir         = output_dir
        self.fac_log_transforms = fac_log_transforms

    def run(self):
        try:
            out_shp, _ = finalize_supply(
                self.sgg_df, self.fac_meta, self.sgg_shp, self.sgg_col,
                self.std_method, self.output_dir, self.fac_log_transforms,
                log_fn=self.log.emit,
            )
            self.finished.emit(out_shp)
        except Exception:
            import traceback
            self.error.emit(traceback.format_exc())


# ── 메인 다이얼로그
class SupplyIndexDialog(QDialog):

    _LOG_KEYS   = ['none', 'ln', 'log10', 'reflected_ln', 'reflected_log10']
    _LOG_LABELS = ['변환 없음', 'ln(x+1)', 'log₁₀(x+1)', '반사 ln', '반사 log₁₀']

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface          = iface
        self.sup_worker     = None
        self.final_worker   = None
        self._scan_results  = []
        self._auto_sectors  = []
        self._custom_mode   = False
        self._row_map       = []
        self._sgg_df        = None
        self._fac_meta      = None
        self._log_combo_map = {}   # fac_name → QComboBox

        self.setWindowTitle("공급수준 분석")
        self.setMinimumWidth(720)
        self.setMinimumHeight(680)
        self._build_ui()

    # ── UI 구성
    def _build_ui(self):
        layout = QVBoxLayout()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab1(), "1단계: 데이터 입력")
        self.tabs.addTab(self._tab2(), "2단계: 부문 분류")
        self.tabs.addTab(self._tab3(), "3단계: 로그 변환")
        self.tabs.addTab(self._tab4(), "4단계: 계산")
        layout.addWidget(self.tabs)

        log_box = QGroupBox("로그")
        log_lay = QVBoxLayout()
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setFixedHeight(130)
        self.log_area.setFont(QFont("Consolas", 9))
        log_lay.addWidget(self.log_area)
        log_box.setLayout(log_lay)
        layout.addWidget(log_box)

        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.close)
        layout.addWidget(btn_close)

        self.setLayout(layout)

    def _path_row(self, label, dir_mode=True, on_change=None):
        row  = QHBoxLayout()
        lbl  = QLabel(label)
        lbl.setFixedWidth(160)
        edit = QLineEdit()
        btn  = QPushButton("찾아보기")
        btn.setFixedWidth(80)

        def browse_dir():
            path = QFileDialog.getExistingDirectory(self, "폴더 선택")
            if path:
                edit.setText(path)
                if on_change:
                    on_change()

        def browse_file():
            path, _ = QFileDialog.getOpenFileName(
                self, "파일 선택", "", "SHP Files (*.shp)")
            if path:
                edit.setText(path)
                if on_change:
                    on_change()

        btn.clicked.connect(browse_dir if dir_mode else browse_file)
        if on_change:
            edit.editingFinished.connect(on_change)

        row.addWidget(lbl)
        row.addWidget(edit)
        row.addWidget(btn)
        return row, edit

    # ── Tab 1: 데이터 입력
    def _tab1(self):
        w = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(10)

        row1, self.edit_point_dir = self._path_row(
            "Point SHP 폴더", dir_mode=True)
        layout.addLayout(row1)

        row2, self.edit_sgg_shp = self._path_row(
            "시군구 경계 SHP", dir_mode=False,
            on_change=self._load_sgg_columns)
        layout.addLayout(row2)

        row3, self.edit_grid_shp = self._path_row(
            "1km 인구 격자 SHP", dir_mode=False,
            on_change=self._load_grid_columns)
        layout.addLayout(row3)

        row4, self.edit_output = self._path_row(
            "출력 폴더", dir_mode=True)
        layout.addLayout(row4)

        # 시군구 컬럼
        sgg_row = QHBoxLayout()
        lbl_sgg = QLabel("시군구 식별 컬럼")
        lbl_sgg.setFixedWidth(160)
        self.combo_sgg = QComboBox()
        self.combo_sgg.setMinimumWidth(220)
        sgg_row.addWidget(lbl_sgg)
        sgg_row.addWidget(self.combo_sgg)
        sgg_row.addStretch()
        layout.addLayout(sgg_row)

        # 격자 인구 컬럼
        gp_row = QHBoxLayout()
        lbl_gp = QLabel("격자 인구 컬럼")
        lbl_gp.setFixedWidth(160)
        self.combo_grid_pop = QComboBox()
        self.combo_grid_pop.setMinimumWidth(220)
        gp_row.addWidget(lbl_gp)
        gp_row.addWidget(self.combo_grid_pop)
        gp_row.addStretch()
        layout.addLayout(gp_row)

        btn_scan = QPushButton("스캔  (시설 목록 불러오기)")
        btn_scan.setMinimumHeight(32)
        btn_scan.clicked.connect(self._scan)
        layout.addWidget(btn_scan)

        self.lbl_scan = QLabel("※ 폴더를 선택 후 스캔하세요.")
        self.lbl_scan.setWordWrap(True)
        layout.addWidget(self.lbl_scan)

        layout.addStretch()
        w.setLayout(layout)
        return w

    # ── Tab 2: 부문 분류
    def _tab2(self):
        w = QWidget()
        layout = QVBoxLayout()

        info = QLabel(
            "파일명에서 부문을 자동 감지합니다.  "
            "미분류(회색)는 [사용자 설정] 버튼 후 행을 더블클릭하여 부문을 지정하세요."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(1)
        self.table.setHorizontalHeaderLabels(["파일명"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        self.btn_custom = QPushButton("사용자 설정  (부문 직접 편집)")
        self.btn_custom.clicked.connect(self._toggle_custom_mode)
        btn_reset = QPushButton("기본값 초기화")
        btn_reset.clicked.connect(self._reset_sectors)
        btn_confirm = QPushButton("분류 확정  →  3단계")
        btn_confirm.setMinimumHeight(32)
        btn_confirm.clicked.connect(self._confirm_classification)
        btn_row.addWidget(self.btn_custom)
        btn_row.addWidget(btn_reset)
        btn_row.addStretch()
        btn_row.addWidget(btn_confirm)
        layout.addLayout(btn_row)

        w.setLayout(layout)
        return w

    # ── Tab 3: 로그 변환
    def _tab3(self):
        w = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(8)

        info = QLabel(
            "거주지 1km²당 시설 수의 분포를 확인하고 시설별 로그 변환 방법을 설정하세요.\n"
            "왜도(skewness) |값| > 1.0 이면 로그 변환을 권장합니다."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table_log = QTableWidget()
        self.table_log.setColumnCount(5)
        self.table_log.setHorizontalHeaderLabels(
            ['시설명', 'N', '평균', '왜도', '로그변환'])
        hh = self.table_log.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 5):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table_log.verticalHeader().setVisible(False)
        self.table_log.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.table_log)

        btn_row = QHBoxLayout()
        btn_reset_log = QPushButton("권장값으로 초기화")
        btn_reset_log.clicked.connect(self._reset_log_transforms)
        btn_confirm_log = QPushButton("로그변환 확정  →  4단계")
        btn_confirm_log.setMinimumHeight(32)
        btn_confirm_log.clicked.connect(self._confirm_log_transforms)
        btn_row.addWidget(btn_reset_log)
        btn_row.addStretch()
        btn_row.addWidget(btn_confirm_log)
        layout.addLayout(btn_row)

        w.setLayout(layout)
        return w

    # ── Tab 4: 계산
    def _tab4(self):
        w = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(12)

        std_box = QGroupBox("표준화 방법")
        std_lay = QVBoxLayout()
        self.combo_std = QComboBox()
        self.combo_std.addItems([
            "Min-Max 정규화  (0 ~ 1)",
            "Z-score 표준화",
            "T점수  (50 + 10Z)",
            "백분위 순위  (Percentile Rank)",
            "표준화 없음  (원값 그대로)",
        ])
        std_lay.addWidget(self.combo_std)
        std_box.setLayout(std_lay)
        layout.addWidget(std_box)

        info = QLabel(
            "계산 순서\n"
            "① 시군구별 거주 격자 수(1km²) 집계  (격자 centroid → 시군구 공간조인)\n"
            "② 시설별 공간조인 → 거주 1km²당 시설 수 (_sup)\n"
            "③ 시설별 로그 변환 적용  (3단계 설정)\n"
            "④ 선택한 방법으로 표준화\n"
            "⑤ 부문 내 시설별 표준화 값의 단순 평균 → 부문 공급수준"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.btn_run = QPushButton("▶  계산 실행")
        self.btn_run.setMinimumHeight(38)
        self.btn_run.clicked.connect(self._run)
        layout.addWidget(self.btn_run)

        layout.addStretch()
        w.setLayout(layout)
        return w

    # ── Tab 1 동작
    def _load_sgg_columns(self):
        path = self.edit_sgg_shp.text().strip()
        if not path or not os.path.exists(path):
            return
        try:
            cols = load_shp_columns(path)
        except Exception as e:
            self._log(f"[경고] SGG 컬럼 로드 실패: {e}")
            return
        self.combo_sgg.clear()
        self.combo_sgg.addItems(cols)
        for col in cols:
            if any(k in col.lower() for k in ['cd', 'code', '코드', 'sgg_cd']):
                self.combo_sgg.setCurrentText(col)
                break

    def _load_grid_columns(self):
        path = self.edit_grid_shp.text().strip()
        if not path or not os.path.exists(path):
            return
        try:
            cols = load_shp_columns(path)
        except Exception as e:
            self._log(f"[경고] 격자 컬럼 로드 실패: {e}")
            return
        self.combo_grid_pop.clear()
        self.combo_grid_pop.addItems(cols)
        for col in cols:
            if any(k in col.lower() for k in ['총인구', '인구수', '합계', 'total', 'pop']):
                self.combo_grid_pop.setCurrentText(col)
                break

    def _scan(self):
        point_dir = self.edit_point_dir.text().strip()
        if not point_dir or not os.path.isdir(point_dir):
            QMessageBox.warning(self, "경고", "Point SHP 폴더를 선택하세요.")
            return

        files = sorted(f for f in os.listdir(point_dir) if f.lower().endswith('.shp'))
        if not files:
            QMessageBox.warning(self, "경고", "SHP 파일이 없습니다.")
            return

        self._scan_results = []
        self._auto_sectors = []
        for fname in files:
            sector, _ = detect_sector(fname)
            stem = os.path.splitext(fname)[0]
            entry = {
                'filepath':     os.path.join(point_dir, fname),
                'sector':       sector or '미분류',
                'display_name': stem,
            }
            self._scan_results.append(entry)
            self._auto_sectors.append(sector or '미분류')

        matched   = sum(1 for s in self._auto_sectors if s != '미분류')
        unmatched = len(self._auto_sectors) - matched
        self.lbl_scan.setText(
            f"감지: {len(files)}개 파일  |  자동 매핑: {matched}개  |  미분류: {unmatched}개"
        )
        self._log(f"스캔 완료: {len(files)}개 (매핑 {matched} / 미분류 {unmatched})")
        self._refresh_table()
        self._custom_mode = False
        self._update_custom_btn()
        self.tabs.setCurrentIndex(1)

    # ── Tab 2 동작
    def _refresh_table(self):
        groups = {}
        for idx, entry in enumerate(self._scan_results):
            groups.setdefault(entry['sector'], []).append(idx)

        self._row_map = []
        for sec, indices in groups.items():
            self._row_map.append((True, None))
            for idx in indices:
                self._row_map.append((False, idx))

        self.table.setRowCount(len(self._row_map))

        HDR_BG  = QColor('#1565c0')
        HDR_FG  = QColor('#ffffff')
        MISC_BG = QColor('#e0e0e0')
        hdr_font = QFont()
        hdr_font.setBold(True)

        tbl_row = 0
        for sec, indices in groups.items():
            hdr_item = QTableWidgetItem(f"  {sec}   ({len(indices)}개)")
            hdr_item.setFont(hdr_font)
            hdr_item.setBackground(HDR_BG)
            hdr_item.setForeground(HDR_FG)
            hdr_item.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(tbl_row, 0, hdr_item)
            self.table.setRowHeight(tbl_row, 26)
            tbl_row += 1

            for scan_idx in indices:
                entry = self._scan_results[scan_idx]
                item = QTableWidgetItem('    ' + entry['display_name'])
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if entry['sector'] == '미분류':
                    item.setBackground(MISC_BG)
                self.table.setItem(tbl_row, 0, item)
                tbl_row += 1

    def _on_cell_double_clicked(self, row, col):
        if not self._custom_mode:
            return
        is_header, scan_idx = self._row_map[row]
        if is_header:
            return
        current = self._scan_results[scan_idx]['sector']
        new_sector, ok = QInputDialog.getText(
            self, "부문 편집", "부문명을 입력하세요:", text=current)
        if ok and new_sector.strip():
            self._scan_results[scan_idx]['sector'] = new_sector.strip()
            self._refresh_table()

    def _toggle_custom_mode(self):
        self._custom_mode = not self._custom_mode
        if not self._custom_mode:
            self._refresh_table()
        self._update_custom_btn()

    def _update_custom_btn(self):
        if self._custom_mode:
            self.btn_custom.setText("사용자 설정 완료  (편집 종료)")
            self.btn_custom.setStyleSheet("background-color: #ffe0b2;")
        else:
            self.btn_custom.setText("사용자 설정  (부문 직접 편집)")
            self.btn_custom.setStyleSheet("")

    def _reset_sectors(self):
        for scan_idx, auto_sec in enumerate(self._auto_sectors):
            self._scan_results[scan_idx]['sector'] = auto_sec
        self._custom_mode = False
        self._update_custom_btn()
        self._refresh_table()
        self._log("부문 기본값으로 초기화")

    def _confirm_classification(self):
        if not self._scan_results:
            QMessageBox.warning(self, "경고", "먼저 1단계에서 스캔하세요.")
            return

        sgg_shp  = self.edit_sgg_shp.text().strip()
        grid_shp = self.edit_grid_shp.text().strip()
        if not sgg_shp or not os.path.exists(sgg_shp):
            QMessageBox.warning(self, "경고", "시군구 경계 SHP를 선택하세요.")
            return
        if not grid_shp or not os.path.exists(grid_shp):
            QMessageBox.warning(self, "경고", "1km 인구 격자 SHP를 선택하세요.")
            return

        sgg_col      = self.combo_sgg.currentText()
        grid_pop_col = self.combo_grid_pop.currentText()
        if not sgg_col or not grid_pop_col:
            QMessageBox.warning(self, "경고", "시군구 컬럼과 격자 인구 컬럼을 선택하세요.")
            return

        still_unmatched = [
            r['display_name'] for r in self._scan_results if r['sector'] == '미분류'
        ]
        if still_unmatched:
            names = '\n'.join(f'  • {n}' for n in still_unmatched[:5])
            extra = f'\n  ... 외 {len(still_unmatched)-5}개' if len(still_unmatched) > 5 else ''
            resp = QMessageBox.question(
                self, "미분류 시설 있음",
                f"아직 미분류인 시설이 있습니다:\n{names}{extra}\n\n"
                "계속 진행하시겠습니까? (미분류 시설도 계산에 포함됩니다)",
                QMessageBox.Yes | QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return

        self._log("부문 분류 확정. 거주 km² 및 시설 공간조인 산출 중...")

        self._progress = QProgressDialog(
            "거주지 면적 및 시설 집계 중...", None, 0, 0, self)
        self._progress.setWindowTitle("산출 중")
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.setMinimumWidth(300)
        self._progress.setCancelButton(None)
        self._progress.show()

        self.sup_worker = SupWorker(
            list(self._scan_results), sgg_shp, grid_shp, grid_pop_col, sgg_col
        )
        self.sup_worker.log.connect(self._log)
        self.sup_worker.finished.connect(self._on_sup_done)
        self.sup_worker.error.connect(self._on_sup_error)
        self.sup_worker.start()

    def _on_sup_done(self, result):
        self._close_progress()
        sgg_df, fac_meta = result
        self._sgg_df   = sgg_df
        self._fac_meta = fac_meta
        self._log("집계 완료. 로그 변환 설정으로 이동합니다.")
        self._refresh_log_table()
        self.tabs.setCurrentIndex(2)

    def _on_sup_error(self, msg):
        self._close_progress()
        self._log(f"[오류]\n{msg}")

    # ── Tab 3 동작
    def _refresh_log_table(self):
        if self._sgg_df is None or self._fac_meta is None:
            return

        prev = {name: cb.currentIndex() for name, cb in self._log_combo_map.items()}
        self._log_combo_map = {}

        self.table_log.setRowCount(len(self._fac_meta))
        WARN_BG = QColor('#fff9c4')

        for row, fm in enumerate(self._fac_meta):
            sup_col = fm['col'] + '_sup'
            if sup_col in self._sgg_df.columns:
                s    = self._sgg_df[sup_col].dropna()
                n    = int(s.count())
                mean = float(s.mean()) if n > 0 else 0.0
                skew = float(s.skew()) if n > 1 else 0.0
            else:
                n, mean, skew = 0, 0.0, 0.0

            if skew > 2.0:
                rec = 'log10'
            elif skew > 1.0:
                rec = 'ln'
            elif skew < -2.0:
                rec = 'reflected_log10'
            elif skew < -1.0:
                rec = 'reflected_ln'
            else:
                rec = 'none'

            self.table_log.setItem(row, 0, QTableWidgetItem(fm['name']))
            self.table_log.setItem(row, 1, QTableWidgetItem(str(n)))
            self.table_log.setItem(row, 2, QTableWidgetItem(f"{mean:.4f}"))

            skew_item = QTableWidgetItem(f"{skew:.3f}")
            if abs(skew) > 1.0:
                skew_item.setBackground(WARN_BG)
            self.table_log.setItem(row, 3, skew_item)

            cb = QComboBox()
            cb.addItems(self._LOG_LABELS)
            if fm['name'] in prev:
                cb.setCurrentIndex(prev[fm['name']])
            else:
                rec_idx = self._LOG_KEYS.index(rec) if rec in self._LOG_KEYS else 0
                cb.setCurrentIndex(rec_idx)
            self.table_log.setCellWidget(row, 4, cb)
            self._log_combo_map[fm['name']] = cb

    def _reset_log_transforms(self):
        if self._sgg_df is None or self._fac_meta is None:
            return
        for fm in self._fac_meta:
            sup_col = fm['col'] + '_sup'
            if sup_col in self._sgg_df.columns:
                s    = self._sgg_df[sup_col].dropna()
                skew = float(s.skew()) if len(s) > 1 else 0.0
            else:
                skew = 0.0

            if skew > 2.0:
                rec = 'log10'
            elif skew > 1.0:
                rec = 'ln'
            elif skew < -2.0:
                rec = 'reflected_log10'
            elif skew < -1.0:
                rec = 'reflected_ln'
            else:
                rec = 'none'

            if fm['name'] in self._log_combo_map:
                rec_idx = self._LOG_KEYS.index(rec) if rec in self._LOG_KEYS else 0
                self._log_combo_map[fm['name']].setCurrentIndex(rec_idx)

    def _confirm_log_transforms(self):
        if not self._log_combo_map:
            QMessageBox.warning(self, "경고", "먼저 2단계 분류 확정을 완료하세요.")
            return
        for fm in self._fac_meta:
            if fm['name'] in self._log_combo_map:
                idx = self._log_combo_map[fm['name']].currentIndex()
                fm['log_transform'] = self._LOG_KEYS[idx]
        self._log("로그 변환 설정 확정.")
        self.tabs.setCurrentIndex(3)

    # ── Tab 4 동작
    def _get_std_method(self):
        return ['minmax', 'zscore', 'tscore', 'percentile', 'none'][
            self.combo_std.currentIndex()]

    def _run(self):
        if self._sgg_df is None:
            QMessageBox.warning(self, "경고", "먼저 2단계 분류 확정을 완료하세요.")
            return

        sgg_shp    = self.edit_sgg_shp.text().strip()
        output_dir = self.edit_output.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "경고", "출력 폴더를 선택하세요.")
            return

        sgg_col    = self.combo_sgg.currentText()
        std_method = self._get_std_method()

        fac_log_transforms = {
            fm['name']: fm.get('log_transform', 'none')
            for fm in self._fac_meta
        }

        self._log(f"\n=== 계산 시작 (표준화: {std_method}) ===")
        self.btn_run.setEnabled(False)

        self._progress = QProgressDialog("계산 중...", None, 0, 0, self)
        self._progress.setWindowTitle("실행 중")
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.setMinimumWidth(280)
        self._progress.setCancelButton(None)
        self._progress.show()

        self.final_worker = FinalWorker(
            self._sgg_df, self._fac_meta, sgg_shp, sgg_col,
            std_method, output_dir, fac_log_transforms,
        )
        self.final_worker.log.connect(self._log)
        self.final_worker.finished.connect(self._on_done)
        self.final_worker.error.connect(self._on_error)
        self.final_worker.start()

    def _on_done(self, out_shp):
        self._close_progress()
        self.btn_run.setEnabled(True)
        if os.path.exists(out_shp):
            layer = QgsVectorLayer(out_shp, 'supply_index', 'ogr')
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self._log("→ QGIS 레이어 추가: supply_index")
        self._log("=== 완료 ===")
        QMessageBox.information(self, "완료", "공급수준 분석이 완료되었습니다.")

    def _on_error(self, msg):
        self._close_progress()
        self.btn_run.setEnabled(True)
        self._log(f"[오류]\n{msg}")

    # ── 공통 헬퍼
    def _close_progress(self):
        if hasattr(self, '_progress') and self._progress:
            self._progress.close()
            self._progress = None

    def _log(self, msg):
        self.log_area.append(msg)
        self.log_area.verticalScrollBar().setValue(
            self.log_area.verticalScrollBar().maximum()
        )
