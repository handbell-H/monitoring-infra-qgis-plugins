import os
import json

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QPushButton, QFileDialog, QTextEdit,
    QComboBox, QGroupBox, QMessageBox, QProgressDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)
from qgis.PyQt.QtCore import QThread, pyqtSignal, Qt
from qgis.PyQt.QtGui import QFont, QColor
from qgis.core import QgsProject, QgsVectorLayer

from .processing_core import (
    DEFAULT_SECTORS, detect_sector, load_shp_columns, run_pipeline,
    extract_facility_name, scan_stats,
)


# ── 백그라운드 워커 ──────────────────────────────────────────
class Worker(QThread):
    log      = pyqtSignal(str)
    finished = pyqtSignal(str)
    error    = pyqtSignal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn     = fn
        self.args   = args
        self.kwargs = kwargs

    def run(self):
        try:
            out_shp, _ = self.fn(*self.args, log_fn=self.log.emit, **self.kwargs)
            self.finished.emit(out_shp)
        except Exception:
            import traceback
            self.error.emit(traceback.format_exc())


# ── 메인 다이얼로그 ──────────────────────────────────────────
class ServicePopDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface          = iface
        self.worker         = None
        self._scan_results  = []
        self._auto_sectors  = []
        self._stats_list    = []
        self._log_combo_map = {}
        self._custom_mode   = False
        self._row_map       = []

        self.setWindowTitle("향유수준 분석")
        self.setMinimumWidth(700)
        self.setMinimumHeight(620)
        self._build_ui()

    # ── UI 구성 ────────────────────────────────────────────
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
        lbl.setFixedWidth(130)
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
            path, _ = QFileDialog.getOpenFileName(self, "파일 선택", "", "SHP Files (*.shp)")
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

    # ── Tab 1 ──────────────────────────────────────────────
    def _tab1(self):
        w = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(10)

        row1, self.edit_shp_dir = self._path_row("서비스권역 SHP 폴더", dir_mode=True)
        layout.addLayout(row1)

        row2, self.edit_output = self._path_row("출력 폴더", dir_mode=True)
        layout.addLayout(row2)

        # 시군구 컬럼 선택
        sgg_row = QHBoxLayout()
        lbl_sgg = QLabel("시군구 식별 컬럼")
        lbl_sgg.setFixedWidth(130)
        self.combo_sgg = QComboBox()
        self.combo_sgg.setMinimumWidth(200)
        self.combo_sgg.addItems(['sgg_cd', 'sgg_nm_k'])   # 기본값 미리 제공
        sgg_row.addWidget(lbl_sgg)
        sgg_row.addWidget(self.combo_sgg)
        sgg_row.addStretch()
        layout.addLayout(sgg_row)

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

    # ── Tab 2 ──────────────────────────────────────────────
    def _tab2(self):
        w = QWidget()
        layout = QVBoxLayout()

        info = QLabel(
            "파일명에서 부문을 자동 감지합니다.  "
            "미분류(회색)는 [사용자 설정] 버튼으로 직접 입력하세요."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["파일명", "부문"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
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

    # ── Tab 3: 로그 변환 ──────────────────────────────────────
    def _tab3(self):
        w = QWidget()
        layout = QVBoxLayout()

        info = QLabel(
            "시설별 분포 통계를 바탕으로 로그 변환 방법을 확인·수정하세요.\n"
            "왜도 > 1: 오른꼬리 → 로그 권장  |  왜도 < -1: 왼꼬리 → 반사로그 권장"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table_log = QTableWidget()
        self.table_log.setColumnCount(5)
        self.table_log.setHorizontalHeaderLabels(["시설명", "N", "평균", "왜도", "로그 변환"])
        self.table_log.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 5):
            self.table_log.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table_log.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_log.verticalHeader().setVisible(False)
        layout.addWidget(self.table_log)

        btn_row = QHBoxLayout()
        btn_reset_log = QPushButton("권장값으로 초기화")
        btn_reset_log.clicked.connect(self._reset_log_transforms)
        btn_confirm_log = QPushButton("변환 확정  →  4단계")
        btn_confirm_log.setMinimumHeight(32)
        btn_confirm_log.clicked.connect(self._confirm_log_transforms)
        btn_row.addWidget(btn_reset_log)
        btn_row.addStretch()
        btn_row.addWidget(btn_confirm_log)
        layout.addLayout(btn_row)

        w.setLayout(layout)
        return w

    # ── Tab 4: 계산 ──────────────────────────────────────────
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
            "① 시설별 서비스권역 내 인구비율(value_r) 읽기\n"
            "② 3단계에서 설정한 시설별 로그 변환 적용\n"
            "   반사로그 선택 시: 표준화 후 방향 자동 역전 복원\n"
            "③ 선택한 방법으로 표준화\n"
            "④ 부문 내 시설별 표준화 값의 단순 평균 → 부문 향유수준"
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

    # ── Tab 1 동작 ────────────────────────────────────────
    def _scan(self):
        shp_dir = self.edit_shp_dir.text().strip()
        if not shp_dir or not os.path.isdir(shp_dir):
            QMessageBox.warning(self, "경고", "SHP 폴더를 선택하세요.")
            return

        files = sorted(f for f in os.listdir(shp_dir) if f.lower().endswith('.shp'))
        if not files:
            QMessageBox.warning(self, "경고", "SHP 파일이 없습니다.")
            return

        # 첫 파일에서 컬럼 로드 → combo_sgg 갱신
        try:
            cols = load_shp_columns(os.path.join(shp_dir, files[0]))
            self.combo_sgg.clear()
            self.combo_sgg.addItems(cols)
            for col in cols:
                if 'cd' in col.lower() or 'code' in col.lower():
                    self.combo_sgg.setCurrentText(col)
                    break
        except Exception as e:
            self._log(f"[경고] 컬럼 로드 실패: {e}")

        self._scan_results = []
        self._auto_sectors = []

        for fname in files:
            fac_name = extract_facility_name(fname)
            display  = fac_name or os.path.splitext(fname)[0]
            sector, _ = detect_sector(fname)
            entry = {
                'filepath':     os.path.join(shp_dir, fname),
                'sector':       sector or '미분류',
                'display_name': display,
            }
            self._scan_results.append(entry)
            self._auto_sectors.append(sector or '미분류')

        matched   = sum(1 for s in self._auto_sectors if s != '미분류')
        unmatched = len(self._auto_sectors) - matched
        self.lbl_scan.setText(
            f"감지: {len(files)}개 파일  |  자동 매핑: {matched}개  |  미분류: {unmatched}개"
        )
        self._log(f"스캔 완료: {len(files)}개 (매핑 {matched} / 미분류 {unmatched})")

        # ── 시설별 분포 통계 계산 및 권장 로그 변환 설정
        self._stats_list = []
        for entry in self._scan_results:
            st = scan_stats(entry['filepath'])
            self._stats_list.append(st)
            rec = 'none'
            if st:
                skew = st['skew']
                if skew > 2.0:    rec = 'log10'
                elif skew > 1.0:  rec = 'ln'
                elif skew < -2.0: rec = 'reflected_log10'
                elif skew < -1.0: rec = 'reflected_ln'
            entry['log_transform']     = rec
            entry['log_transform_rec'] = rec
        self._log("시설별 분포 통계 계산 완료  (3단계에서 로그 변환 확인)")

        self._custom_mode = False
        self._update_custom_btn()
        self._refresh_table()
        self.tabs.setCurrentIndex(1)

    # ── Tab 2 동작 ────────────────────────────────────────
    def _sync_from_table(self):
        for tbl_row, (is_header, scan_idx) in enumerate(self._row_map):
            if is_header:
                continue
            item = self.table.item(tbl_row, 1)
            if item:
                self._scan_results[scan_idx]['sector'] = item.text().strip()

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

        HDR_BG   = QColor('#1565c0')
        HDR_FG   = QColor('#ffffff')
        MISC_BG  = QColor('#e0e0e0')
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
            hdr_item2 = QTableWidgetItem('')
            hdr_item2.setBackground(HDR_BG)
            hdr_item2.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(tbl_row, 1, hdr_item2)
            self.table.setSpan(tbl_row, 0, 1, 2)
            self.table.setRowHeight(tbl_row, 26)
            tbl_row += 1

            for scan_idx in indices:
                entry = self._scan_results[scan_idx]
                item_fname = QTableWidgetItem('    ' + entry['display_name'])
                item_fname.setFlags(item_fname.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(tbl_row, 0, item_fname)

                item_sec = QTableWidgetItem(entry['sector'])
                if entry['sector'] == '미분류':
                    item_sec.setBackground(MISC_BG)
                self.table.setItem(tbl_row, 1, item_sec)
                tbl_row += 1

        trigger = (QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
                   if self._custom_mode else QAbstractItemView.NoEditTriggers)
        self.table.setEditTriggers(trigger)

    def _toggle_custom_mode(self):
        if self._custom_mode:
            self._sync_from_table()
            self._custom_mode = False
            self._refresh_table()
        else:
            self._custom_mode = True
        self._update_custom_btn()
        if self._custom_mode:
            self.table.setEditTriggers(
                QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked)

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
        self._sync_from_table()

        still_unmatched = [r['display_name'] for r in self._scan_results
                           if r['sector'] == '미분류']
        if still_unmatched:
            names = '\n'.join(f'  • {n}' for n in still_unmatched[:5])
            extra = f'\n  ... 외 {len(still_unmatched)-5}개' if len(still_unmatched) > 5 else ''
            resp = QMessageBox.question(
                self, "미분류 시설 있음",
                f"아직 미분류인 시설이 있습니다:\n{names}{extra}\n\n계속 진행하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return

        self._log("부문 분류 확정 완료.")
        self._refresh_log_table()
        self.tabs.setCurrentIndex(2)

    # ── Tab 3 동작 ────────────────────────────────────────
    _LOG_KEYS = ['none', 'ln', 'log10', 'reflected_ln', 'reflected_log10']
    _LOG_LABELS = [
        '변환 없음',
        '자연로그  ln(x+1)',
        '상용로그  log10(x+1)',
        '반사 자연로그  ln(max+1-x)',
        '반사 상용로그  log10(max+1-x)',
    ]

    def _refresh_log_table(self):
        groups = {}
        for idx, entry in enumerate(self._scan_results):
            groups.setdefault(entry['sector'], []).append(idx)

        total_rows = sum(1 + len(v) for v in groups.values())
        self.table_log.setRowCount(total_rows)
        self._log_combo_map = {}

        HDR_BG   = QColor('#1565c0')
        HDR_FG   = QColor('#ffffff')
        hdr_font = QFont()
        hdr_font.setBold(True)

        tbl_row = 0
        for sec, indices in groups.items():
            for c in range(5):
                item = QTableWidgetItem(f"  {sec}   ({len(indices)}개)" if c == 0 else '')
                item.setFont(hdr_font)
                item.setBackground(HDR_BG)
                item.setForeground(HDR_FG)
                item.setFlags(Qt.ItemIsEnabled)
                self.table_log.setItem(tbl_row, c, item)
            self.table_log.setSpan(tbl_row, 0, 1, 5)
            self.table_log.setRowHeight(tbl_row, 26)
            tbl_row += 1

            for scan_idx in indices:
                entry = self._scan_results[scan_idx]
                st = self._stats_list[scan_idx] if scan_idx < len(self._stats_list) else None

                self.table_log.setItem(tbl_row, 0, QTableWidgetItem('    ' + entry['display_name']))

                if st:
                    self.table_log.setItem(tbl_row, 1, QTableWidgetItem(str(st['n'])))
                    self.table_log.setItem(tbl_row, 2, QTableWidgetItem(f"{st['mean']:.3f}"))
                    skew_item = QTableWidgetItem(f"{st['skew']:+.2f}")
                    if abs(st['skew']) > 1.0:
                        skew_item.setForeground(QColor('#c62828'))
                    self.table_log.setItem(tbl_row, 3, skew_item)
                else:
                    for c in range(1, 4):
                        self.table_log.setItem(tbl_row, c, QTableWidgetItem('-'))

                combo = QComboBox()
                for label in self._LOG_LABELS:
                    combo.addItem(label)
                lt = entry.get('log_transform', 'none')
                combo.setCurrentIndex(self._LOG_KEYS.index(lt) if lt in self._LOG_KEYS else 0)
                self.table_log.setCellWidget(tbl_row, 4, combo)
                self._log_combo_map[scan_idx] = combo
                tbl_row += 1

    def _confirm_log_transforms(self):
        for scan_idx, combo in self._log_combo_map.items():
            self._scan_results[scan_idx]['log_transform'] = self._LOG_KEYS[combo.currentIndex()]
        self._log("로그 변환 설정 확정.")
        self.tabs.setCurrentIndex(3)

    def _reset_log_transforms(self):
        for scan_idx, combo in self._log_combo_map.items():
            rec = self._scan_results[scan_idx].get('log_transform_rec', 'none')
            combo.setCurrentIndex(self._LOG_KEYS.index(rec) if rec in self._LOG_KEYS else 0)
        self._log("로그 변환 권장값으로 초기화")

    # ── Tab 4 동작 ────────────────────────────────────────
    def _run(self):
        if not self._scan_results:
            QMessageBox.warning(self, "경고", "먼저 1단계 스캔을 완료하세요.")
            return

        output_dir = self.edit_output.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "경고", "출력 폴더를 선택하세요.")
            return

        sgg_col       = self.combo_sgg.currentText()
        std_method = ['minmax', 'zscore', 'tscore', 'percentile', 'none'][self.combo_std.currentIndex()]

        self._log(f"\n=== 계산 시작 (표준화: {std_method}) ===")
        self.btn_run.setEnabled(False)

        self._progress = QProgressDialog("계산 중...", None, 0, 0, self)
        self._progress.setWindowTitle("실행 중")
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.setMinimumWidth(280)
        self._progress.setCancelButton(None)
        self._progress.show()

        self.worker = Worker(
            run_pipeline,
            list(self._scan_results), sgg_col, std_method, output_dir,
        )
        self.worker.log.connect(self._log)
        self.worker.finished.connect(self._on_done)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _on_done(self, out_shp):
        self._close_progress()
        self.btn_run.setEnabled(True)
        if os.path.exists(out_shp):
            layer = QgsVectorLayer(out_shp, 'service_pop_index', 'ogr')
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self._log("→ QGIS 레이어 추가: service_pop_index")
        self._log("=== 완료 ===")
        QMessageBox.information(self, "완료", "향유수준 분석이 완료되었습니다.")

    def _on_error(self, msg):
        self._close_progress()
        self.btn_run.setEnabled(True)
        self._log(f"[오류]\n{msg}")

    def _close_progress(self):
        if hasattr(self, '_progress') and self._progress:
            self._progress.close()
            self._progress = None

    def _log(self, msg):
        self.log_area.append(msg)
        self.log_area.verticalScrollBar().setValue(
            self.log_area.verticalScrollBar().maximum()
        )
