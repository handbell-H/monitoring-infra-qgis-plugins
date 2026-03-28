import os

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
    DEFAULT_SECTORS, detect_sector, get_default_threshold,
    extract_facility_name, run_pipeline,
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
class AccessIdxDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface           = iface
        self.worker          = None
        self._scan_results   = []   # {filepath, sector, display_name, threshold, thr_confirmed}
        self._auto_sectors   = []
        self._custom_mode    = False
        self._row_map        = []   # Tab 2 table: (is_header, scan_idx)
        self._thr_row_map    = []   # Tab 3 table: (is_header, scan_idx)
        self._unknown_warned = False  # Tab 3 경고창 1회 표시 여부

        self.setWindowTitle("충족수준 분석")
        self.setMinimumWidth(720)
        self.setMinimumHeight(660)
        self._build_ui()

    # ── UI 구성 ────────────────────────────────────────────
    def _build_ui(self):
        layout = QVBoxLayout()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab1(), "1단계: 데이터 입력")
        self.tabs.addTab(self._tab2(), "2단계: 부문 분류")
        self.tabs.addTab(self._tab3(), "3단계: 거리 기준")
        self.tabs.addTab(self._tab4(), "4단계: 계산")
        self.tabs.currentChanged.connect(self._on_tab_changed)
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
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(155)
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

    # ── Tab 1 ──────────────────────────────────────────────
    def _tab1(self):
        w = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(10)

        row1, self.edit_access_dir = self._path_row("접근성 SHP 폴더", dir_mode=True)
        layout.addLayout(row1)

        row2, self.edit_sgg_shp = self._path_row(
            "시군구 경계 SHP", dir_mode=False,
            on_change=self._load_sgg_columns)
        layout.addLayout(row2)

        row3, self.edit_output = self._path_row("출력 폴더", dir_mode=True)
        layout.addLayout(row3)

        sgg_row = QHBoxLayout()
        lbl_sgg = QLabel("시군구 식별 컬럼")
        lbl_sgg.setFixedWidth(155)
        self.combo_sgg = QComboBox()
        self.combo_sgg.setMinimumWidth(220)
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
            "미분류(회색)는 [사용자 설정] 버튼 후 행을 더블클릭하여 부문을 지정하세요."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table2 = QTableWidget()
        self.table2.setColumnCount(1)
        self.table2.setHorizontalHeaderLabels(["파일명"])
        self.table2.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table2.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table2.verticalHeader().setVisible(False)
        self.table2.cellDoubleClicked.connect(self._on_table2_double_clicked)
        layout.addWidget(self.table2)

        btn_row = QHBoxLayout()
        self.btn_custom = QPushButton("사용자 설정  (부문 직접 편집)")
        self.btn_custom.clicked.connect(self._toggle_custom_mode)
        btn_reset = QPushButton("기본값 초기화")
        btn_reset.clicked.connect(self._reset_sectors)
        btn_confirm2 = QPushButton("분류 확정  →  3단계")
        btn_confirm2.setMinimumHeight(32)
        btn_confirm2.clicked.connect(self._confirm_classification)
        btn_row.addWidget(self.btn_custom)
        btn_row.addWidget(btn_reset)
        btn_row.addStretch()
        btn_row.addWidget(btn_confirm2)
        layout.addLayout(btn_row)

        w.setLayout(layout)
        return w

    # ── Tab 3 ──────────────────────────────────────────────
    def _tab3(self):
        w = QWidget()
        layout = QVBoxLayout()

        info = QLabel(
            "시설별 접근성 충족 거리 기준(km)을 확인·수정하세요.  "
            "거리(km) 셀을 더블클릭하면 편집할 수 있습니다.  "
            "주황색 행은 미인식 시설로, 반드시 직접 확인해야 4단계로 진행됩니다."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table3 = QTableWidget()
        self.table3.setColumnCount(2)
        self.table3.setHorizontalHeaderLabels(["시설명", "거리 기준 (km)"])
        self.table3.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table3.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table3.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table3.verticalHeader().setVisible(False)
        self.table3.cellDoubleClicked.connect(self._on_table3_double_clicked)
        layout.addWidget(self.table3)

        btn_row = QHBoxLayout()
        btn_reset_thr = QPushButton("디폴트로 초기화")
        btn_reset_thr.clicked.connect(self._reset_thresholds)
        btn_confirm3 = QPushButton("거리 기준 확정  →  4단계")
        btn_confirm3.setMinimumHeight(32)
        btn_confirm3.clicked.connect(self._confirm_thresholds)
        btn_row.addWidget(btn_reset_thr)
        btn_row.addStretch()
        btn_row.addWidget(btn_confirm3)
        layout.addLayout(btn_row)

        w.setLayout(layout)
        return w

    # ── Tab 4 ──────────────────────────────────────────────
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
            "백분위 순위  (Percentile Rank)",
            "표준화 없음  (원값 그대로)",
        ])
        std_lay.addWidget(self.combo_std)
        std_box.setLayout(std_lay)
        layout.addWidget(std_box)

        info = QLabel(
            "계산 순서\n"
            "① 격자별 접근성 SHP 읽기 → gid 기준 병합\n"
            "② 시설별 거리 기준으로 이진화  (≤기준 → 1, 초과 → 0)\n"
            "③ 부문별 합산  →  50% 이상 충족 격자 판정\n"
            "④ 시군구별 충족 격자 비율 산출\n"
            "⑤ 표준화 후 시군구 경계 SHP와 병합하여 저장"
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

    # ── Tab 전환 콜백 ─────────────────────────────────────
    def _on_tab_changed(self, idx):
        if idx == 2 and self._scan_results:
            self._refresh_threshold_table()
            # 미인식 시설이 있을 때 최초 1회 경고
            if not self._unknown_warned:
                unknowns = [r['display_name'] for r in self._scan_results
                            if not r['thr_confirmed']]
                if unknowns:
                    names = '\n'.join(f'  • {n}' for n in unknowns[:8])
                    extra = f'\n  ... 외 {len(unknowns)-8}개' if len(unknowns) > 8 else ''
                    QMessageBox.warning(
                        self, "미인식 시설 — 거리 기준 확인 필요",
                        f"디폴트 목록에 없는 시설이 있습니다 (주황색 행):\n{names}{extra}\n\n"
                        "거리 기준(km) 셀을 더블클릭하여 값을 직접 확인·설정해 주세요.\n"
                        "모든 주황색 행을 설정해야 4단계로 진행할 수 있습니다."
                    )
                    self._unknown_warned = True

    # ── Tab 1 동작 ────────────────────────────────────────
    def _load_sgg_columns(self):
        path = self.edit_sgg_shp.text().strip()
        if not path or not os.path.exists(path):
            return
        try:
            layer = QgsVectorLayer(path, '__tmp__', 'ogr')
            if not layer.isValid():
                return
            cols = [f.name() for f in layer.fields()]
        except Exception:
            return

        self.combo_sgg.clear()
        self.combo_sgg.addItems(cols)
        for col in cols:
            if col.lower() in ('sgg_cd', 'sgg_code'):
                self.combo_sgg.setCurrentText(col)
                break

    def _scan(self):
        access_dir = self.edit_access_dir.text().strip()
        if not access_dir or not os.path.isdir(access_dir):
            QMessageBox.warning(self, "경고", "접근성 SHP 폴더를 선택하세요.")
            return

        files = sorted(f for f in os.listdir(access_dir) if f.lower().endswith('.shp'))
        if not files:
            QMessageBox.warning(self, "경고", "SHP 파일이 없습니다.")
            return

        self._scan_results   = []
        self._auto_sectors   = []
        self._unknown_warned = False  # 새 스캔 시 경고 초기화

        for fname in files:
            fac_name = extract_facility_name(fname)
            sector, _ = detect_sector(fname)
            display = fac_name or os.path.splitext(fname)[0]
            thr, confirmed = get_default_threshold(display)
            entry = {
                'filepath':      os.path.join(access_dir, fname),
                'sector':        sector or '미분류',
                'display_name':  display,
                'threshold':     thr,
                'thr_confirmed': confirmed,
            }
            self._scan_results.append(entry)
            self._auto_sectors.append(sector or '미분류')

        matched   = sum(1 for s in self._auto_sectors if s != '미분류')
        unmatched = len(self._auto_sectors) - matched
        unknown   = sum(1 for r in self._scan_results if not r['thr_confirmed'])
        self.lbl_scan.setText(
            f"감지: {len(files)}개 파일  |  자동 매핑: {matched}개  |  "
            f"미분류: {unmatched}개  |  거리기준 미인식: {unknown}개"
        )
        self._log(f"스캔 완료: {len(files)}개  (거리기준 미인식: {unknown}개)")

        self._refresh_table2()
        self._custom_mode = False
        self._update_custom_btn()
        self.tabs.setCurrentIndex(1)

    # ── Tab 2 동작 ────────────────────────────────────────
    def _refresh_table2(self):
        groups = {}
        for idx, entry in enumerate(self._scan_results):
            groups.setdefault(entry['sector'], []).append(idx)

        self._row_map = []
        for sec, indices in groups.items():
            self._row_map.append((True, None))
            for idx in indices:
                self._row_map.append((False, idx))

        self.table2.setRowCount(len(self._row_map))

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
            self.table2.setItem(tbl_row, 0, hdr_item)
            self.table2.setRowHeight(tbl_row, 26)
            tbl_row += 1

            for scan_idx in indices:
                entry = self._scan_results[scan_idx]
                item = QTableWidgetItem('    ' + entry['display_name'])
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if entry['sector'] == '미분류':
                    item.setBackground(MISC_BG)
                self.table2.setItem(tbl_row, 0, item)
                tbl_row += 1

    def _on_table2_double_clicked(self, row, col):
        if not self._custom_mode:
            return
        is_header, scan_idx = self._row_map[row]
        if is_header:
            return
        current = self._scan_results[scan_idx]['sector']
        new_sector, ok = QInputDialog.getText(
            self, "부문 편집", "부문명을 입력하세요:", text=current
        )
        if ok and new_sector.strip():
            self._scan_results[scan_idx]['sector'] = new_sector.strip()
            self._refresh_table2()

    def _toggle_custom_mode(self):
        self._custom_mode = not self._custom_mode
        self._update_custom_btn()
        if not self._custom_mode:
            self._refresh_table2()

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
        self._refresh_table2()
        self._log("부문 기본값으로 초기화")

    def _confirm_classification(self):
        if not self._scan_results:
            QMessageBox.warning(self, "경고", "먼저 1단계에서 스캔하세요.")
            return
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
        self._log("부문 분류 확정.")
        self.tabs.setCurrentIndex(2)

    # ── Tab 3 동작 ────────────────────────────────────────
    def _refresh_threshold_table(self):
        groups = {}
        for idx, entry in enumerate(self._scan_results):
            groups.setdefault(entry['sector'], []).append(idx)

        self._thr_row_map = []
        for sec, indices in groups.items():
            self._thr_row_map.append((True, None))
            for idx in indices:
                self._thr_row_map.append((False, idx))

        self.table3.setRowCount(len(self._thr_row_map))

        HDR_BG    = QColor('#1565c0')
        HDR_FG    = QColor('#ffffff')
        UNKN_BG   = QColor('#ffe0b2')   # 주황: 미인식 시설
        hdr_font  = QFont()
        hdr_font.setBold(True)

        tbl_row = 0
        for sec, indices in groups.items():
            hdr_item = QTableWidgetItem(f"  {sec}   ({len(indices)}개)")
            hdr_item.setFont(hdr_font)
            hdr_item.setBackground(HDR_BG)
            hdr_item.setForeground(HDR_FG)
            hdr_item.setFlags(Qt.ItemIsEnabled)
            self.table3.setItem(tbl_row, 0, hdr_item)
            hdr_item2 = QTableWidgetItem('')
            hdr_item2.setBackground(HDR_BG)
            hdr_item2.setFlags(Qt.ItemIsEnabled)
            self.table3.setItem(tbl_row, 1, hdr_item2)
            self.table3.setSpan(tbl_row, 0, 1, 2)
            self.table3.setRowHeight(tbl_row, 26)
            tbl_row += 1

            for scan_idx in indices:
                entry = self._scan_results[scan_idx]
                confirmed = entry['thr_confirmed']

                item_name = QTableWidgetItem('    ' + entry['display_name'])
                item_name.setFlags(item_name.flags() & ~Qt.ItemIsEditable)
                if not confirmed:
                    item_name.setBackground(UNKN_BG)
                self.table3.setItem(tbl_row, 0, item_name)

                thr_text = f"{entry['threshold']}  {'✓' if confirmed else '← 확인 필요'}"
                item_thr = QTableWidgetItem(thr_text)
                item_thr.setFlags(item_thr.flags() & ~Qt.ItemIsEditable)
                item_thr.setTextAlignment(Qt.AlignCenter)
                if not confirmed:
                    item_thr.setBackground(UNKN_BG)
                self.table3.setItem(tbl_row, 1, item_thr)
                tbl_row += 1

    def _on_table3_double_clicked(self, row, col):
        if col != 1:
            return
        is_header, scan_idx = self._thr_row_map[row]
        if is_header:
            return
        entry = self._scan_results[scan_idx]
        val, ok = QInputDialog.getDouble(
            self, "거리 기준 편집",
            f"{entry['display_name']}  거리 기준 (km):",
            value=entry['threshold'], min=0.1, max=100.0, decimals=1,
        )
        if ok:
            entry['threshold']     = round(val, 1)
            entry['thr_confirmed'] = True
            self._refresh_threshold_table()

    def _reset_thresholds(self):
        for entry in self._scan_results:
            thr, confirmed = get_default_threshold(entry['display_name'])
            entry['threshold']     = thr
            entry['thr_confirmed'] = confirmed
        self._refresh_threshold_table()
        self._log("거리 기준 디폴트로 초기화")

    def _confirm_thresholds(self):
        """미인식 시설이 모두 확인됐을 때만 4단계로 이동."""
        unconfirmed = [r['display_name'] for r in self._scan_results
                       if not r['thr_confirmed']]
        if unconfirmed:
            names = '\n'.join(f'  • {n}' for n in unconfirmed[:8])
            extra = f'\n  ... 외 {len(unconfirmed)-8}개' if len(unconfirmed) > 8 else ''
            QMessageBox.warning(
                self, "거리 기준 미설정",
                f"아래 시설의 거리 기준이 아직 설정되지 않았습니다 (주황색 행):\n{names}{extra}\n\n"
                "거리 기준(km) 셀을 더블클릭하여 값을 확인·설정한 후 진행하세요."
            )
            return
        self._log("거리 기준 확정.")
        self.tabs.setCurrentIndex(3)

    # ── Tab 4 동작 ────────────────────────────────────────
    def _get_std_method(self):
        return ['minmax', 'zscore', 'percentile', 'none'][self.combo_std.currentIndex()]

    def _run(self):
        if not self._scan_results:
            QMessageBox.warning(self, "경고", "먼저 1단계 스캔을 완료하세요.")
            return

        # 미확인 시설 재확인
        unconfirmed = [r['display_name'] for r in self._scan_results
                       if not r['thr_confirmed']]
        if unconfirmed:
            QMessageBox.warning(
                self, "거리 기준 미설정",
                "3단계에서 미인식 시설의 거리 기준을 먼저 설정해 주세요."
            )
            self.tabs.setCurrentIndex(2)
            return

        sgg_shp = self.edit_sgg_shp.text().strip()
        if not sgg_shp or not os.path.exists(sgg_shp):
            QMessageBox.warning(self, "경고", "시군구 경계 SHP를 선택하세요.")
            return

        output_dir = self.edit_output.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "경고", "출력 폴더를 선택하세요.")
            return

        sgg_col = self.combo_sgg.currentText()
        if not sgg_col:
            QMessageBox.warning(self, "경고", "시군구 식별 컬럼을 선택하세요.")
            return

        std_method = self._get_std_method()
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
            list(self._scan_results), sgg_shp, sgg_col, std_method, output_dir,
        )
        self.worker.log.connect(self._log)
        self.worker.finished.connect(self._on_done)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _on_done(self, out_shp):
        self._close_progress()
        self.btn_run.setEnabled(True)
        if os.path.exists(out_shp):
            layer = QgsVectorLayer(out_shp, 'access_index', 'ogr')
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self._log("→ QGIS 레이어 추가: access_index")
        self._log("=== 완료 ===")
        QMessageBox.information(self, "완료", "충족수준 분석이 완료되었습니다.")

    def _on_error(self, msg):
        self._close_progress()
        self.btn_run.setEnabled(True)
        self._log(f"[오류]\n{msg}")

    # ── 공통 헬퍼 ────────────────────────────────────────
    def _close_progress(self):
        if hasattr(self, '_progress') and self._progress:
            self._progress.close()
            self._progress = None

    def _log(self, msg):
        self.log_area.append(msg)
        self.log_area.verticalScrollBar().setValue(
            self.log_area.verticalScrollBar().maximum()
        )
