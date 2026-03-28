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
    DEFAULT_SECTORS, detect_sector, load_shp_columns, run_pipeline,
)


# ── 백그라운드 워커 ──────────────────────────────────────────
class Worker(QThread):
    log      = pyqtSignal(str)
    finished = pyqtSignal(str)   # out_shp path
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
class SupplyIndexDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface          = iface
        self.worker         = None
        self._scan_results  = []   # list of {filepath, sector, display_name}
        self._auto_sectors  = []   # 자동 감지 결과 (초기화용)
        self._custom_mode   = False
        self._row_map       = []   # (is_header, scan_idx) per table row

        self.setWindowTitle("공급수준 분석")
        self.setMinimumWidth(700)
        self.setMinimumHeight(640)
        self._build_ui()

    # ── UI 구성 ────────────────────────────────────────────
    def _build_ui(self):
        layout = QVBoxLayout()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab1(), "1단계: 데이터 입력")
        self.tabs.addTab(self._tab2(), "2단계: 부문 분류")
        self.tabs.addTab(self._tab3(), "3단계: 계산")
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
        """경로 입력 행 (라벨 + LineEdit + 찾아보기 버튼)."""
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(145)
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

        row1, self.edit_point_dir = self._path_row("Point SHP 폴더", dir_mode=True)
        layout.addLayout(row1)

        row2, self.edit_pop_shp = self._path_row(
            "인구 SHP (시군구 경계)", dir_mode=False,
            on_change=self._load_pop_columns)
        layout.addLayout(row2)

        row3, self.edit_output = self._path_row("출력 폴더", dir_mode=True)
        layout.addLayout(row3)

        # 시군구 컬럼 선택
        sgg_row = QHBoxLayout()
        lbl_sgg = QLabel("시군구 식별 컬럼")
        lbl_sgg.setFixedWidth(145)
        self.combo_sgg = QComboBox()
        self.combo_sgg.setMinimumWidth(220)
        sgg_row.addWidget(lbl_sgg)
        sgg_row.addWidget(self.combo_sgg)
        sgg_row.addStretch()
        layout.addLayout(sgg_row)

        # 인구 컬럼 선택
        pop_row = QHBoxLayout()
        lbl_pop = QLabel("인구 컬럼")
        lbl_pop.setFixedWidth(145)
        self.combo_pop = QComboBox()
        self.combo_pop.setMinimumWidth(220)
        pop_row.addWidget(lbl_pop)
        pop_row.addWidget(self.combo_pop)
        pop_row.addStretch()
        layout.addLayout(pop_row)

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

    # ── Tab 3 ──────────────────────────────────────────────
    def _tab3(self):
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
            "① 시설별로 1천인당 시설 수 산출  (시군구 단위 공간조인)\n"
            "② 선택한 방법으로 표준화\n"
            "③ 부문 내 시설별 표준화 값의 단순 평균 → 부문 공급수준"
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
    def _load_pop_columns(self):
        path = self.edit_pop_shp.text().strip()
        if not path or not os.path.exists(path):
            return
        try:
            cols = load_shp_columns(path)
        except Exception as e:
            self._log(f"[경고] 컬럼 로드 실패: {e}")
            return

        self.combo_sgg.clear()
        self.combo_pop.clear()
        self.combo_sgg.addItems(cols)
        self.combo_pop.addItems(cols)

        # 휴리스틱 기본 선택
        for col in cols:
            cl = col.lower()
            if any(k in cl for k in ['cd', 'code', '코드', 'sgg_cd']):
                self.combo_sgg.setCurrentText(col)
                break
        for col in cols:
            cl = col.lower()
            if any(k in cl for k in ['총인구', '인구수', '합계', 'total', 'pop']):
                self.combo_pop.setCurrentText(col)
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

        matched = sum(1 for s in self._auto_sectors if s != '미분류')
        unmatched = len(self._auto_sectors) - matched
        self.lbl_scan.setText(
            f"감지: {len(files)}개 파일  |  자동 매핑: {matched}개  |  미분류: {unmatched}개"
        )
        self._log(f"스캔 완료: {len(files)}개 (매핑 {matched} / 미분류 {unmatched})")

        self._refresh_table()
        self._custom_mode = False
        self._update_custom_btn()
        self.tabs.setCurrentIndex(1)

    # ── Tab 2 동작 ────────────────────────────────────────
    def _sync_from_table(self):
        """부문 편집은 더블클릭 즉시 반영되므로 별도 동기화 불필요."""
        pass

    def _refresh_table(self):
        """부문별 그룹 헤더 행 + 시설 행으로 테이블 재구성."""
        groups = {}
        for idx, entry in enumerate(self._scan_results):
            sec = entry['sector']
            groups.setdefault(sec, []).append(idx)

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
            # ── 그룹 헤더 행
            hdr_text = f"  {sec}   ({len(indices)}개)"
            hdr_item = QTableWidgetItem(hdr_text)
            hdr_item.setFont(hdr_font)
            hdr_item.setBackground(HDR_BG)
            hdr_item.setForeground(HDR_FG)
            hdr_item.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(tbl_row, 0, hdr_item)
            self.table.setRowHeight(tbl_row, 26)
            tbl_row += 1

            # ── 시설 데이터 행
            for scan_idx in indices:
                entry = self._scan_results[scan_idx]
                item_fname = QTableWidgetItem('    ' + entry['display_name'])
                item_fname.setFlags(item_fname.flags() & ~Qt.ItemIsEditable)
                if entry['sector'] == '미분류':
                    item_fname.setBackground(MISC_BG)
                self.table.setItem(tbl_row, 0, item_fname)
                tbl_row += 1

    def _on_cell_double_clicked(self, row, col):
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
            self._refresh_table()

    def _toggle_custom_mode(self):
        if self._custom_mode:
            self._custom_mode = False
            self._refresh_table()
        else:
            self._custom_mode = True
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

        self._sync_from_table()

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

        self._log("부문 분류 확정 완료.")
        self.tabs.setCurrentIndex(2)

    # ── Tab 3 동작 ────────────────────────────────────────
    def _get_std_method(self):
        return ['minmax', 'zscore', 'percentile', 'none'][self.combo_std.currentIndex()]

    def _run(self):
        if not self._scan_results:
            QMessageBox.warning(self, "경고", "먼저 1단계 스캔을 완료하세요.")
            return

        pop_shp = self.edit_pop_shp.text().strip()
        if not pop_shp or not os.path.exists(pop_shp):
            QMessageBox.warning(self, "경고", "인구 SHP를 선택하세요.")
            return

        output_dir = self.edit_output.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "경고", "출력 폴더를 선택하세요.")
            return

        sgg_col = self.combo_sgg.currentText()
        pop_col = self.combo_pop.currentText()
        if not sgg_col or not pop_col:
            QMessageBox.warning(self, "경고", "시군구 컬럼과 인구 컬럼을 선택하세요.")
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
            list(self._scan_results), pop_shp, sgg_col, pop_col,
            std_method, output_dir,
        )
        self.worker.log.connect(self._log)
        self.worker.finished.connect(self._on_done)
        self.worker.error.connect(self._on_error)
        self.worker.start()

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
