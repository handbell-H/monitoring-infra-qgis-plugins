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

from .processing_core import SECTORS, run_pipeline

DEFAULT_INPUT_WEIGHT = 1 / 3
DEFAULT_SECTOR_WEIGHT = 0.2


# ── 백그라운드 워커 ──────────────────────────────────────────
class Worker(QThread):
    log      = pyqtSignal(str)
    finished = pyqtSignal(str)
    error    = pyqtSignal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def run(self):
        try:
            out_shp, _ = self.fn(*self.args, log_fn=self.log.emit, **self.kwargs)
            self.finished.emit(out_shp)
        except Exception:
            import traceback
            self.error.emit(traceback.format_exc())


# ── 메인 다이얼로그 ──────────────────────────────────────────
class CompositeDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface  = iface
        self.worker = None

        # 가중치 저장소
        # input_weights[sec_col] = {'sup': float, 'pop': float, 'acc': float}
        self._input_weights  = {s['col']: {'sup': DEFAULT_INPUT_WEIGHT,
                                            'pop': DEFAULT_INPUT_WEIGHT,
                                            'acc': DEFAULT_INPUT_WEIGHT}
                                 for s in SECTORS}
        # sector_weights[sec_col] = float
        self._sector_weights = {s['col']: DEFAULT_SECTOR_WEIGHT for s in SECTORS}

        self.setWindowTitle("생활인프라 편리성 종합지수")
        self.setMinimumWidth(740)
        self.setMinimumHeight(660)
        self._build_ui()

    # ── UI 구성 ────────────────────────────────────────────
    def _build_ui(self):
        layout = QVBoxLayout()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab1(), "1단계: 데이터 입력")
        self.tabs.addTab(self._tab2(), "2단계: 부문별 입력 가중치")
        self.tabs.addTab(self._tab3(), "3단계: 부문 가중치")
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

    def _path_row(self, label, dir_mode=False):
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(160)
        edit = QLineEdit()
        btn  = QPushButton("찾아보기")
        btn.setFixedWidth(80)

        def browse():
            if dir_mode:
                path = QFileDialog.getExistingDirectory(self, "폴더 선택")
            else:
                path, _ = QFileDialog.getOpenFileName(self, "SHP 파일 선택", "", "SHP Files (*.shp)")
            if path:
                edit.setText(path)

        btn.clicked.connect(browse)
        row.addWidget(lbl)
        row.addWidget(edit)
        row.addWidget(btn)
        return row, edit

    # ── Tab 1: 데이터 입력 ───────────────────────────────────
    def _tab1(self):
        w = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(10)

        row1, self.edit_supply     = self._path_row("공급수준 SHP")
        row2, self.edit_service    = self._path_row("향유수준 SHP")
        row3, self.edit_access     = self._path_row("충족수준 SHP")
        row4, self.edit_output     = self._path_row("출력 폴더", dir_mode=True)
        for r in [row1, row2, row3, row4]:
            layout.addLayout(r)

        sgg_row = QHBoxLayout()
        lbl_sgg = QLabel("시군구 식별 컬럼")
        lbl_sgg.setFixedWidth(160)
        self.edit_sgg_col = QLineEdit("sgg_cd")
        self.edit_sgg_col.setMaximumWidth(180)
        sgg_row.addWidget(lbl_sgg)
        sgg_row.addWidget(self.edit_sgg_col)
        sgg_row.addStretch()
        layout.addLayout(sgg_row)

        info = QLabel(
            "※ 공급수준(supply_index.shp) · 향유수준(service_pop_index.shp) · "
            "충족수준(access_index.shp) 를 각각 선택하세요."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        layout.addStretch()
        w.setLayout(layout)
        return w

    # ── Tab 2: 부문별 입력 가중치 ────────────────────────────
    def _tab2(self):
        w = QWidget()
        layout = QVBoxLayout()

        info = QLabel(
            "부문별로 공급수준 · 향유수준 · 충족수준 세 값의 가중치를 설정합니다.\n"
            "각 행의 합계가 1.00이 되도록 설정해 주세요.  셀을 더블클릭하면 편집됩니다."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table2 = QTableWidget()
        self.table2.setColumnCount(5)
        self.table2.setHorizontalHeaderLabels(["부문", "공급수준", "향유수준", "충족수준", "합계"])
        self.table2.setRowCount(len(SECTORS))
        self.table2.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 5):
            self.table2.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table2.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table2.verticalHeader().setVisible(False)
        self.table2.cellDoubleClicked.connect(self._on_table2_double_clicked)
        layout.addWidget(self.table2)

        btn_row = QHBoxLayout()
        btn_reset2 = QPushButton("균등 초기화  (각 0.333)")
        btn_reset2.clicked.connect(self._reset_input_weights)
        btn_row.addWidget(btn_reset2)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        w.setLayout(layout)
        self._refresh_table2()
        return w

    # ── Tab 3: 부문 가중치 ───────────────────────────────────
    def _tab3(self):
        w = QWidget()
        layout = QVBoxLayout()

        info = QLabel(
            "5개 부문 편리성의 최종 합산 가중치를 설정합니다.\n"
            "가중치 합계가 1.00이 되어야 합니다.  셀을 더블클릭하면 편집됩니다."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table3 = QTableWidget()
        self.table3.setColumnCount(2)
        self.table3.setHorizontalHeaderLabels(["부문 편리성", "가중치"])
        self.table3.setRowCount(len(SECTORS) + 1)   # +1: 합계 행
        self.table3.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table3.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table3.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table3.verticalHeader().setVisible(False)
        self.table3.cellDoubleClicked.connect(self._on_table3_double_clicked)
        layout.addWidget(self.table3)

        btn_row = QHBoxLayout()
        btn_reset3 = QPushButton("균등 초기화  (각 0.200)")
        btn_reset3.clicked.connect(self._reset_sector_weights)
        btn_row.addWidget(btn_reset3)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        w.setLayout(layout)
        self._refresh_table3()
        return w

    # ── Tab 4: 계산 ─────────────────────────────────────────
    def _tab4(self):
        w = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(12)

        info = QLabel(
            "계산 순서\n"
            "① 공급수준 · 향유수준 · 충족수준 SHP 로드 및 시군구 기준 병합\n"
            "② 부문별 편리성 = 공급수준×w1 + 향유수준×w2 + 충족수준×w3\n"
            "③ 생활인프라 편리성 = Σ (부문 편리성 × 부문 가중치)\n"
            "④ 0 ~ 100 rescaling 후 시군구 SHP로 저장"
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

    # ── Tab 2 동작 ────────────────────────────────────────
    def _refresh_table2(self):
        SUM_OK  = QColor('#c8e6c9')   # 녹색: 합계 ≈ 1.0
        SUM_ERR = QColor('#ffcdd2')   # 빨강: 합계 ≠ 1.0
        bold = QFont(); bold.setBold(True)

        for row, sec in enumerate(SECTORS):
            wts = self._input_weights[sec['col']]
            s   = wts['sup'] + wts['pop'] + wts['acc']

            item0 = QTableWidgetItem(sec['label'])
            item0.setFlags(item0.flags() & ~Qt.ItemIsEditable)
            item0.setFont(bold)
            self.table2.setItem(row, 0, item0)

            for col, key in enumerate(['sup', 'pop', 'acc'], start=1):
                item = QTableWidgetItem(f"{wts[key]:.3f}")
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter)
                self.table2.setItem(row, col, item)

            sum_item = QTableWidgetItem(f"{s:.3f}")
            sum_item.setFlags(sum_item.flags() & ~Qt.ItemIsEditable)
            sum_item.setTextAlignment(Qt.AlignCenter)
            sum_item.setBackground(SUM_OK if abs(s - 1.0) < 0.01 else SUM_ERR)
            self.table2.setItem(row, 4, sum_item)

    def _on_table2_double_clicked(self, row, col):
        if col not in (1, 2, 3):
            return
        key_map = {1: 'sup', 2: 'pop', 3: 'acc'}
        key = key_map[col]
        sec = SECTORS[row]
        current = self._input_weights[sec['col']][key]
        label_map = {1: '공급수준', 2: '향유수준', 3: '충족수준'}
        val, ok = QInputDialog.getDouble(
            self, "가중치 편집",
            f"{sec['label']}  —  {label_map[col]} 가중치:",
            value=current, min=0.0, max=1.0, decimals=3,
        )
        if ok:
            self._input_weights[sec['col']][key] = round(val, 3)
            self._refresh_table2()

    def _reset_input_weights(self):
        for s in SECTORS:
            self._input_weights[s['col']] = {
                'sup': round(DEFAULT_INPUT_WEIGHT, 3),
                'pop': round(DEFAULT_INPUT_WEIGHT, 3),
                'acc': round(DEFAULT_INPUT_WEIGHT, 3),
            }
        self._refresh_table2()

    # ── Tab 3 동작 ────────────────────────────────────────
    def _refresh_table3(self):
        SUM_OK  = QColor('#c8e6c9')
        SUM_ERR = QColor('#ffcdd2')
        bold = QFont(); bold.setBold(True)

        total = sum(self._sector_weights.values())

        for row, sec in enumerate(SECTORS):
            w = self._sector_weights[sec['col']]

            item0 = QTableWidgetItem(sec['label'])
            item0.setFlags(item0.flags() & ~Qt.ItemIsEditable)
            self.table3.setItem(row, 0, item0)

            item1 = QTableWidgetItem(f"{w:.3f}")
            item1.setFlags(item1.flags() & ~Qt.ItemIsEditable)
            item1.setTextAlignment(Qt.AlignCenter)
            self.table3.setItem(row, 1, item1)

        # 합계 행
        sum_row = len(SECTORS)
        item_lbl = QTableWidgetItem("합  계")
        item_lbl.setFlags(item_lbl.flags() & ~Qt.ItemIsEditable)
        item_lbl.setFont(bold)
        self.table3.setItem(sum_row, 0, item_lbl)

        item_sum = QTableWidgetItem(f"{total:.3f}")
        item_sum.setFlags(item_sum.flags() & ~Qt.ItemIsEditable)
        item_sum.setTextAlignment(Qt.AlignCenter)
        item_sum.setFont(bold)
        item_sum.setBackground(SUM_OK if abs(total - 1.0) < 0.01 else SUM_ERR)
        self.table3.setItem(sum_row, 1, item_sum)

    def _on_table3_double_clicked(self, row, col):
        if col != 1 or row >= len(SECTORS):
            return
        sec = SECTORS[row]
        current = self._sector_weights[sec['col']]
        val, ok = QInputDialog.getDouble(
            self, "가중치 편집",
            f"{sec['label']} 가중치:",
            value=current, min=0.0, max=1.0, decimals=3,
        )
        if ok:
            self._sector_weights[sec['col']] = round(val, 3)
            self._refresh_table3()

    def _reset_sector_weights(self):
        for s in SECTORS:
            self._sector_weights[s['col']] = DEFAULT_SECTOR_WEIGHT
        self._refresh_table3()

    # ── Tab 4 동작 ────────────────────────────────────────
    def _validate_weights(self):
        """가중치 합계 검증. 문제 있으면 경고 후 False 반환."""
        # 부문별 입력 가중치
        bad_input = []
        for s in SECTORS:
            wts = self._input_weights[s['col']]
            total = wts['sup'] + wts['pop'] + wts['acc']
            if abs(total - 1.0) > 0.01:
                bad_input.append(f"  • {s['label']}: {total:.3f}")
        if bad_input:
            QMessageBox.warning(
                self, "입력 가중치 오류",
                "아래 부문의 입력 가중치 합계가 1.00이 아닙니다:\n" +
                '\n'.join(bad_input) + "\n\n2단계에서 수정해 주세요."
            )
            return False

        # 부문 가중치
        sec_total = sum(self._sector_weights.values())
        if abs(sec_total - 1.0) > 0.01:
            QMessageBox.warning(
                self, "부문 가중치 오류",
                f"부문 가중치 합계가 {sec_total:.3f}입니다. 1.00이 되도록 3단계에서 수정해 주세요."
            )
            return False
        return True

    def _run(self):
        supply_shp  = self.edit_supply.text().strip()
        service_shp = self.edit_service.text().strip()
        access_shp  = self.edit_access.text().strip()
        output_dir  = self.edit_output.text().strip()
        sgg_col     = self.edit_sgg_col.text().strip() or 'sgg_cd'

        for path, name in [(supply_shp, '공급수준 SHP'),
                           (service_shp, '향유수준 SHP'),
                           (access_shp,  '충족수준 SHP')]:
            if not path or not os.path.exists(path):
                QMessageBox.warning(self, "경고", f"{name}를 선택하세요.")
                return
        if not output_dir:
            QMessageBox.warning(self, "경고", "출력 폴더를 선택하세요.")
            return

        if not self._validate_weights():
            return

        self._log("\n=== 계산 시작 ===")
        self.btn_run.setEnabled(False)

        self._progress = QProgressDialog("계산 중...", None, 0, 0, self)
        self._progress.setWindowTitle("실행 중")
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.setMinimumWidth(280)
        self._progress.setCancelButton(None)
        self._progress.show()

        self.worker = Worker(
            run_pipeline,
            supply_shp, service_shp, access_shp,
            sgg_col,
            {s['col']: dict(self._input_weights[s['col']]) for s in SECTORS},
            dict(self._sector_weights),
            output_dir,
        )
        self.worker.log.connect(self._log)
        self.worker.finished.connect(self._on_done)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _on_done(self, out_shp):
        self._close_progress()
        self.btn_run.setEnabled(True)
        if os.path.exists(out_shp):
            layer = QgsVectorLayer(out_shp, 'composite_index', 'ogr')
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self._log("→ QGIS 레이어 추가: composite_index")
        self._log("=== 완료 ===")
        QMessageBox.information(self, "완료", "생활인프라 편리성 종합지수 산출이 완료되었습니다.")

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
