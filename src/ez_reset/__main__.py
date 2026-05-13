from __future__ import annotations

import contextlib
import logging
import re
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

from ez_reset.d4 import D4ControlBackend
from ez_reset.devices import UnsupportedPrinterError, by_model
from ez_reset.printer import Printer
from ez_reset.status import ConsumableLevel, ConsumableStatus, InkColor, InkLevel, PrinterError, PrinterState
from ez_reset.utils import parse_identifier
from ez_reset.win_usbprint import USBPRINTTransport, enumerate_printers


LOG_PATH = Path.cwd() / "ez-reset.log"
SLOW_TASK_MS = 5000
DEFAULT_SLOW_HINT = "设备响应较慢。若长时间无结果，请重新插拔 USB 后再试。"
VID_PID_PATTERN = re.compile(r"VID_([0-9A-F]{4}).*PID_([0-9A-F]{4})", re.IGNORECASE)

INK_COLOR_NAMES = {
    InkColor.BLACK: "黑色",
    InkColor.CYAN: "青色",
    InkColor.MAGENTA: "洋红",
    InkColor.YELLOW: "黄色",
    InkColor.LIGHT_CYAN: "浅青",
    InkColor.LIGHT_MAGENTA: "浅红",
    InkColor.DARK_YELLOW: "深黄",
    InkColor.GRAY: "灰色",
    InkColor.LIGHT_BLACK: "浅黑",
    InkColor.RED: "红色",
    InkColor.BLUE: "蓝色",
    InkColor.GLOSS_OPTIMIZER: "光泽优化",
    InkColor.LIGHT_GRAY: "浅灰",
    InkColor.ORANGE: "橙色",
    InkColor.UNKNOWN: "未知颜色",
}

INK_COLOR_SWATCHES = {
    InkColor.BLACK: "#202124",
    InkColor.CYAN: "#00a0e9",
    InkColor.MAGENTA: "#d81b60",
    InkColor.YELLOW: "#f4c20d",
    InkColor.LIGHT_CYAN: "#80deea",
    InkColor.LIGHT_MAGENTA: "#f48fb1",
    InkColor.DARK_YELLOW: "#c49000",
    InkColor.GRAY: "#9aa0a6",
    InkColor.LIGHT_BLACK: "#616161",
    InkColor.RED: "#db4437",
    InkColor.BLUE: "#1a73e8",
    InkColor.GLOSS_OPTIMIZER: "#90caf9",
    InkColor.LIGHT_GRAY: "#d7d7d7",
    InkColor.ORANGE: "#f29900",
    InkColor.UNKNOWN: "#bdbdbd",
}

PRINTER_STATE_NAMES = {
    PrinterState.ERROR: "错误",
    PrinterState.SELF_PRINTING: "自检打印",
    PrinterState.BUSY: "忙碌",
    PrinterState.WAITING: "等待中",
    PrinterState.IDLE: "空闲",
    PrinterState.PAUSE: "暂停",
    PrinterState.INKDRYING: "墨水干燥中",
    PrinterState.CLEANING: "清洗中",
    PrinterState.FACTORY_SHIPMENT: "出厂模式",
    PrinterState.MOTOR_DRIVE_OFF: "电机关闭",
    PrinterState.SHUTDOWN: "已关机",
    PrinterState.WAITPAPERINIT: "等待进纸初始化",
    PrinterState.INIT_PAPER: "初始化纸路",
}

PRINTER_ERROR_NAMES = {
    PrinterError.NONE: "无",
    PrinterError.FATAL: "致命错误",
    PrinterError.INTERFACE: "接口错误",
    PrinterError.PAPERJAM: "卡纸",
    PrinterError.INKOUT: "墨量不足",
    PrinterError.PAPEROUT: "缺纸",
    PrinterError.PAPERSIZE: "纸张尺寸错误",
    PrinterError.PAPERPATH: "纸路错误",
    PrinterError.SERVICEREQ: "需要维护",
    PrinterError.DOUBLEFEED: "多张进纸",
    PrinterError.INKCOVEROPEN: "墨仓盖打开",
    PrinterError.NOMAINTENANCEBOX: "未安装维护盒",
    PrinterError.COVEROPEN: "机盖打开",
    PrinterError.NOTRAY: "纸盒未安装",
    PrinterError.CARDLOADING: "卡片加载中",
    PrinterError.CDDVDCONFIG: "光盘托架配置错误",
    PrinterError.CARTRIDGEOVERFLOW: "墨盒溢出",
    PrinterError.BATTERYVOLTAGE: "电池电压异常",
    PrinterError.BATTERYTEMPERATURE: "电池温度异常",
    PrinterError.BATTERYEMPTY: "电池耗尽",
    PrinterError.SHUTOFF: "设备已自动关机",
    PrinterError.NOT_INITIALFILL: "未完成初始化灌墨",
    PrinterError.PRINTPACKEND: "打印包结束",
    PrinterError.MAINTENANCEBOXCOVEROPEN: "维护盒盖打开",
    PrinterError.SCANNEROPEN: "扫描单元打开",
    PrinterError.CDRGUIDEOPEN: "光盘导轨打开",
    PrinterError.CDREXIST: "检测到光盘",
    PrinterError.CDREXIST_MAINTE: "维护模式下检测到光盘",
    PrinterError.TRAYCLOSE: "托盘未关闭",
}

CONSUMABLE_STATUS_NAMES = {
    ConsumableStatus.OKAY: "正常",
    ConsumableStatus.EMPTY: "已空",
    ConsumableStatus.MISSING: "未安装",
    ConsumableStatus.FAIL: "故障",
    ConsumableStatus.UNKNOWN: "未知",
}


def configure_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    try:
        handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
    except OSError:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )


configure_logging()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrinterCandidate:
    path: str
    label: str
    vid: str
    pid: str

    @property
    def vid_pid(self) -> str:
        if self.vid == "----" and self.pid == "----":
            return "未知"

        return f"{self.vid}:{self.pid}"


@dataclass(frozen=True)
class PrinterSnapshot:
    path: str
    description: str
    model: str
    serial: str
    state: PrinterState
    error: PrinterError
    maintenance_box: ConsumableLevel
    ink_levels: list[InkLevel]
    waste_levels: list[tuple[int, int]]


class PrinterSession:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._transport: USBPRINTTransport | None = None
        self._backend: D4ControlBackend | None = None
        self._printer: Printer | None = None
        self._identifier: dict[str, str] = {}

    def open(self) -> PrinterSnapshot:
        with self._lock:
            if self._printer is not None:
                return self._snapshot()

            transport = USBPRINTTransport(self.path).__enter__()
            backend = None

            try:
                backend = D4ControlBackend(transport).__enter__()
                identifier = parse_identifier(backend.identify())
                model = identifier.get("MDL")
                if not model:
                    msg = "设备没有返回可识别的 MDL 型号字段。"
                    raise RuntimeError(msg)

                printer = Printer(backend, device=by_model(model))

                self._transport = transport
                self._backend = backend
                self._printer = printer
                self._identifier = identifier

                return self._snapshot()
            except Exception:
                if backend is not None:
                    with contextlib.suppress(Exception):
                        backend.__exit__(None, None, None)

                with contextlib.suppress(Exception):
                    transport.__exit__(None, None, None)

                self._transport = None
                self._backend = None
                self._printer = None
                self._identifier = {}
                raise

    def reconnect(self) -> PrinterSnapshot:
        self.close()
        return self.open()

    def refresh(self) -> PrinterSnapshot:
        with self._lock:
            return self._snapshot()

    def reset_waste(self) -> PrinterSnapshot | None:
        with self._lock:
            if self._printer is None:
                msg = "打印机尚未连接。"
                raise RuntimeError(msg)

            if not self._printer.device.reset:
                msg = "当前机型没有可用的废墨清零地址。"
                raise UnsupportedPrinterError(msg)

            self._printer.reset_waste()

            try:
                return self._snapshot()
            except Exception:
                logger.warning("Refresh after waste reset failed for %s", self.path, exc_info=True)
                return None

    def close(self) -> None:
        if not self._lock.acquire(timeout=0.2):
            logger.warning("Skipping device close because the session is busy: %s", self.path)
            return

        try:
            if self._backend is not None:
                with contextlib.suppress(Exception):
                    self._backend.__exit__(None, None, None)

            if self._transport is not None:
                with contextlib.suppress(Exception):
                    self._transport.__exit__(None, None, None)

            self._transport = None
            self._backend = None
            self._printer = None
            self._identifier = {}
        finally:
            self._lock.release()

    def _snapshot(self) -> PrinterSnapshot:
        if self._printer is None:
            msg = "打印机会话尚未建立。"
            raise RuntimeError(msg)

        status = self._printer.get_status()
        waste_levels = list(self._printer.get_waste())
        model = self._identifier.get("MDL", "未知")
        description = self._identifier.get("DES") or model
        serial = status.serial or self._identifier.get("SN", "")

        return PrinterSnapshot(
            path=self.path,
            description=description,
            model=model,
            serial=serial,
            state=status.state,
            error=status.error,
            maintenance_box=status.maintenance_box,
            ink_levels=list(status.levels),
            waste_levels=waste_levels,
        )


class InkLevelView(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        ttk.Frame.__init__(self, master, padding=(6, 4))

        self.columnconfigure(1, weight=1)

        self.swatch = tk.Canvas(self, width=14, height=14, highlightthickness=0)
        self.swatch.grid(row=0, column=0, rowspan=2, padx=(0, 8), sticky="n")

        self.name_var = tk.StringVar(value="墨水")
        self.value_var = tk.StringVar(value="--")

        self.name_label = ttk.Label(self, textvariable=self.name_var)
        self.name_label.grid(row=0, column=1, sticky="w")

        self.value_label = ttk.Label(self, textvariable=self.value_var)
        self.value_label.grid(row=0, column=2, padx=(8, 0), sticky="e")

        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(4, 0))

    def update_level(self, level: InkLevel) -> None:
        self.name_var.set(INK_COLOR_NAMES.get(level.color, level.color.name.title()))
        self.value_var.set(format_consumable_level(level))
        self.progress["value"] = max(0, min(level.level, 100)) if level.level >= 0 else 0

        self.swatch.delete("all")
        color = INK_COLOR_SWATCHES.get(level.color, INK_COLOR_SWATCHES[InkColor.UNKNOWN])
        self.swatch.create_oval(1, 1, 13, 13, fill=color, outline=color)


class WasteLevelView(ttk.Frame):
    def __init__(self, master: tk.Misc, label: str) -> None:
        ttk.Frame.__init__(self, master, padding=(6, 4))

        self.columnconfigure(0, weight=1)

        self.label_var = tk.StringVar(value=label)
        self.value_var = tk.StringVar(value="--")

        ttk.Label(self, textvariable=self.label_var).grid(row=0, column=0, sticky="w")
        ttk.Label(self, textvariable=self.value_var).grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

    def update_level(self, level: int, max_level: int) -> None:
        percent, text = format_waste_level(level, max_level)
        self.progress["value"] = percent
        self.value_var.set(text)


class PrinterWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc, candidate: PrinterCandidate) -> None:
        tk.Toplevel.__init__(self, master)

        self.candidate = candidate
        self.session = PrinterSession(candidate.path)
        self._busy = False
        self._connected = False
        self._closed = False
        self._slow_task_id: str | None = None

        self.title(f"{candidate.label} | ez-reset")
        self.geometry("860x760")
        self.minsize(760, 620)
        self.protocol("WM_DELETE_WINDOW", self.close_window)

        self.status_var = tk.StringVar(value="准备连接设备…")
        self.name_var = tk.StringVar(value="正在识别…")
        self.model_var = tk.StringVar(value="--")
        self.serial_var = tk.StringVar(value="--")
        self.state_var = tk.StringVar(value="--")
        self.error_var = tk.StringVar(value="--")
        self.maintenance_var = tk.StringVar(value="--")
        self.path_var = tk.StringVar(value=candidate.path)

        self.ink_views: list[InkLevelView] = []
        self.waste_views: list[WasteLevelView] = []

        self._build_ui()
        self.after(50, self.connect_printer)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        container = ttk.Frame(self, padding=12)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(5, weight=1)

        header = ttk.Frame(container)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(
            header,
            textvariable=self.name_var,
            font=("Segoe UI", 14, "bold"),
        ).grid(row=0, column=0, sticky="w")

        ttk.Label(
            header,
            text=f"设备编号：{self.candidate.label}    VID/PID：{self.candidate.vid_pid}",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        ttk.Label(
            container,
            textvariable=self.status_var,
            foreground="#0f4c81",
        ).grid(row=1, column=0, sticky="ew", pady=(10, 6))

        self.progress = ttk.Progressbar(container, mode="indeterminate")
        self.progress.grid(row=2, column=0, sticky="ew")

        info_frame = ttk.LabelFrame(container, text="设备信息", padding=10)
        info_frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        info_frame.columnconfigure(1, weight=1)

        info_rows = (
            ("型号", self.model_var),
            ("序列号", self.serial_var),
            ("打印机状态", self.state_var),
            ("当前错误", self.error_var),
            ("维护盒", self.maintenance_var),
            ("USBPRINT 路径", self.path_var),
        )

        for row, (label, variable) in enumerate(info_rows):
            ttk.Label(info_frame, text=f"{label}：").grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=3)
            ttk.Label(
                info_frame,
                textvariable=variable,
                wraplength=560,
                justify="left",
            ).grid(row=row, column=1, sticky="ew", pady=3)

        self.ink_frame = ttk.LabelFrame(container, text="墨量", padding=10)
        self.ink_frame.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.ink_frame.columnconfigure(0, weight=1)
        self.ink_frame.columnconfigure(1, weight=1)

        self.waste_frame = ttk.LabelFrame(container, text="废墨计数器", padding=10)
        self.waste_frame.grid(row=5, column=0, sticky="nsew", pady=(12, 0))
        self.waste_frame.columnconfigure(0, weight=1)

        actions = ttk.Frame(container)
        actions.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure(0, weight=1)

        self.reconnect_button = ttk.Button(actions, text="重新连接", command=self.reconnect_printer)
        self.reconnect_button.grid(row=0, column=0, sticky="w")

        self.refresh_button = ttk.Button(actions, text="刷新状态", command=self.refresh_status)
        self.refresh_button.grid(row=0, column=1, padx=(8, 0))

        self.reset_button = ttk.Button(actions, text="废墨计数器清零", command=self.reset_waste)
        self.reset_button.grid(row=0, column=2, padx=(8, 0))

        ttk.Button(actions, text="关闭窗口", command=self.close_window).grid(row=0, column=3, padx=(8, 0))

        self._update_action_state()

    def connect_printer(self) -> None:
        self._run_task(
            operation=self.session.open,
            on_success=self._handle_snapshot,
            busy_text="正在连接打印机…",
            error_title="连接失败",
            disconnect_on_error=True,
        )

    def reconnect_printer(self) -> None:
        self._run_task(
            operation=self.session.reconnect,
            on_success=self._handle_snapshot,
            busy_text="正在重新连接打印机…",
            error_title="重连失败",
            disconnect_on_error=True,
        )

    def refresh_status(self) -> None:
        self._run_task(
            operation=self.session.refresh,
            on_success=self._handle_snapshot,
            busy_text="正在读取打印机状态…",
            error_title="刷新失败",
            disconnect_on_error=True,
        )

    def reset_waste(self) -> None:
        confirmed = messagebox.askyesno(
            "确认清零",
            "确认要重置废墨计数器吗？\n\n请先确保打印机电源稳定、USB 连接正常。",
            parent=self,
        )
        if not confirmed:
            return

        self._run_task(
            operation=self.session.reset_waste,
            on_success=self._handle_reset_success,
            busy_text="正在写入废墨清零数据…",
            error_title="清零失败",
            disconnect_on_error=True,
            slow_hint="清零耗时较长时，请勿断电。若设备无响应，请重新插拔 USB 后重试。",
        )

    def close_window(self) -> None:
        self._closed = True
        self.withdraw()
        threading.Thread(target=self.session.close, daemon=True).start()
        self.destroy()

    def _handle_snapshot(self, snapshot: PrinterSnapshot) -> None:
        self._connected = True
        self.title(f"{snapshot.description} | ez-reset")
        self.name_var.set(snapshot.description)
        self.model_var.set(snapshot.model)
        self.serial_var.set(snapshot.serial or "未提供")
        self.state_var.set(PRINTER_STATE_NAMES.get(snapshot.state, snapshot.state.name))
        self.error_var.set(PRINTER_ERROR_NAMES.get(snapshot.error, snapshot.error.name))
        self.maintenance_var.set(format_consumable_level(snapshot.maintenance_box))
        self.path_var.set(snapshot.path)
        self.status_var.set("设备已连接，可刷新状态或执行废墨清零。")

        self._render_ink_levels(snapshot.ink_levels)
        self._render_waste_levels(snapshot.waste_levels)
        self._update_action_state()

    def _handle_reset_success(self, snapshot: PrinterSnapshot | None) -> None:
        if snapshot is not None:
            self._handle_snapshot(snapshot)

        self.status_var.set("废墨计数器清零指令已发送，建议按提示重启打印机。")
        messagebox.showinfo(
            "清零完成",
            "废墨计数器清零指令已写入。\n\n请重启打印机，然后再次点击“刷新状态”确认结果。",
            parent=self,
        )

    def _render_ink_levels(self, levels: list[InkLevel]) -> None:
        for child in self.ink_frame.winfo_children():
            child.destroy()

        self.ink_views = []

        if not levels:
            ttk.Label(self.ink_frame, text="当前设备没有返回墨量信息。").grid(row=0, column=0, sticky="w")
            return

        for index, level in enumerate(levels):
            view = InkLevelView(self.ink_frame)
            view.update_level(level)
            view.grid(row=index // 2, column=index % 2, sticky="ew", padx=4, pady=4)
            self.ink_views.append(view)

    def _render_waste_levels(self, levels: list[tuple[int, int]]) -> None:
        for child in self.waste_frame.winfo_children():
            child.destroy()

        self.waste_views = []

        if not levels:
            ttk.Label(self.waste_frame, text="当前设备没有返回废墨计数器信息。").grid(row=0, column=0, sticky="w")
            return

        for index, (level, max_level) in enumerate(levels):
            view = WasteLevelView(self.waste_frame, f"废墨计数器 {index + 1}")
            view.update_level(level, max_level)
            view.grid(row=index, column=0, sticky="ew", padx=4, pady=4)
            self.waste_views.append(view)

    def _run_task(
        self,
        operation: Callable[[], PrinterSnapshot | None],
        on_success: Callable[[PrinterSnapshot | None], None],
        busy_text: str,
        error_title: str,
        disconnect_on_error: bool,
        slow_hint: str = DEFAULT_SLOW_HINT,
    ) -> None:
        if self._busy or self._closed:
            return

        self._busy = True
        self.status_var.set(busy_text)
        self._update_action_state()
        self.progress.start(10)

        if self._slow_task_id is not None:
            self.after_cancel(self._slow_task_id)

        self._slow_task_id = self.after(
            SLOW_TASK_MS,
            lambda: self.status_var.set(f"{busy_text} {slow_hint}"),
        )

        def worker() -> None:
            try:
                result = operation()
            except Exception as exc:
                logger.exception("%s: %s", error_title, self.candidate.path)
                self._post_to_ui(lambda exc=exc: self._handle_task_error(error_title, exc, disconnect_on_error))
            else:
                self._post_to_ui(lambda result=result: self._handle_task_success(result, on_success))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_task_success(
        self,
        result: PrinterSnapshot | None,
        on_success: Callable[[PrinterSnapshot | None], None],
    ) -> None:
        if self._closed:
            threading.Thread(target=self.session.close, daemon=True).start()
            return

        self._finish_task()
        on_success(result)

    def _handle_task_error(self, title: str, exc: Exception, disconnect_on_error: bool) -> None:
        if self._closed:
            return

        if disconnect_on_error:
            self.session.close()
            self._connected = False

        self._finish_task()
        self.status_var.set(explain_exception(exc))
        self._update_action_state()

        messagebox.showerror(
            title,
            format_error_dialog(exc),
            parent=self,
        )

    def _finish_task(self) -> None:
        self._busy = False
        self.progress.stop()

        if self._slow_task_id is not None:
            self.after_cancel(self._slow_task_id)
            self._slow_task_id = None

        self._update_action_state()

    def _update_action_state(self) -> None:
        is_ready = self._connected and not self._busy
        reconnect_state = tk.DISABLED if self._busy else tk.NORMAL
        ready_state = tk.NORMAL if is_ready else tk.DISABLED

        self.reconnect_button.configure(state=reconnect_state)
        self.refresh_button.configure(state=ready_state)
        self.reset_button.configure(state=ready_state)

    def _post_to_ui(self, callback: Callable[[], None]) -> None:
        try:
            self.after(0, callback)
        except tk.TclError:
            pass


class PrinterBrowser(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        ttk.Frame.__init__(self, master, padding=12)

        self._candidates: dict[str, PrinterCandidate] = {}

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        ttk.Label(
            self,
            text="Epson 废墨清零工具",
            font=("Segoe UI", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")

        ttk.Label(
            self,
            text="双击设备即可打开详情页。若连接耗时较长，窗口仍可操作，可尝试重新插拔 USB 后重连。",
        ).grid(row=1, column=0, sticky="w", pady=(4, 12))

        table_frame = ttk.Frame(self)
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            table_frame,
            columns=("device", "vidpid", "path"),
            show="headings",
            selectmode="browse",
            height=12,
        )
        self.tree.heading("device", text="设备")
        self.tree.heading("vidpid", text="VID/PID")
        self.tree.heading("path", text="USBPRINT 路径")
        self.tree.column("device", width=150, anchor="w")
        self.tree.column("vidpid", width=100, anchor="center")
        self.tree.column("path", width=620, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-Button-1>", self.open_selected_printer)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_open_button())

        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        action_frame = ttk.Frame(self)
        action_frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        action_frame.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="正在扫描 USBPRINT 设备…")
        ttk.Label(action_frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

        ttk.Button(action_frame, text="刷新设备列表", command=self.update_printers).grid(row=0, column=1, padx=(8, 0))
        self.open_button = ttk.Button(action_frame, text="打开选中设备", command=self.open_selected_printer)
        self.open_button.grid(row=0, column=2, padx=(8, 0))

        self._update_open_button()
        self.update_printers()

    def update_printers(self) -> None:
        previous_selection = self.tree.selection()
        previous_path = previous_selection[0] if previous_selection else None

        try:
            paths = list(enumerate_printers())
        except Exception as exc:
            logger.exception("Failed to enumerate USB printers")
            self.status_var.set("扫描设备失败。")
            messagebox.showerror("扫描失败", format_error_dialog(exc), parent=self.winfo_toplevel())
            return

        self._candidates.clear()

        for item in self.tree.get_children():
            self.tree.delete(item)

        for index, path in enumerate(paths, start=1):
            vid, pid = parse_vid_pid(path)
            candidate = PrinterCandidate(path=path, label=f"USB 打印设备 {index}", vid=vid, pid=pid)
            self._candidates[path] = candidate
            self.tree.insert("", "end", iid=path, values=(candidate.label, candidate.vid_pid, candidate.path))

        if previous_path and previous_path in self._candidates:
            self.tree.selection_set(previous_path)
            self.tree.focus(previous_path)

        count = len(paths)
        self.status_var.set(f"已发现 {count} 个 USBPRINT 设备。" if count else "未发现 USBPRINT 设备。")
        self._update_open_button()

    def open_selected_printer(self, _event: tk.Event | None = None) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("请选择设备", "请先选中一个 USBPRINT 设备。", parent=self.winfo_toplevel())
            return

        candidate = self._candidates[selection[0]]
        PrinterWindow(self.winfo_toplevel(), candidate)

    def _update_open_button(self) -> None:
        has_selection = bool(self.tree.selection())
        self.open_button.configure(state=tk.NORMAL if has_selection else tk.DISABLED)


class EzResetApp(tk.Tk):
    def __init__(self) -> None:
        tk.Tk.__init__(self)

        self.title("ez-reset | Epson 废墨清零工具")
        self.geometry("980x520")
        self.minsize(860, 460)

        style = ttk.Style(self)
        for theme_name in ("vista", "xpnative", "winnative", "clam"):
            if theme_name in style.theme_names():
                style.theme_use(theme_name)
                break

        browser = PrinterBrowser(self)
        browser.pack(fill="both", expand=True)

    def report_callback_exception(
        self,
        exc: type[BaseException],
        val: BaseException,
        tb,
    ) -> None:
        logger.exception("Unhandled Tk exception", exc_info=(exc, val, tb))

        message = f"程序出现未处理异常：{val}"
        if LOG_PATH.exists():
            message = f"{message}\n\n详细日志：{LOG_PATH}"

        messagebox.showerror("程序异常", message, parent=self)


def parse_vid_pid(path: str) -> tuple[str, str]:
    match = VID_PID_PATTERN.search(path)
    if match is None:
        return "----", "----"

    return match.group(1).upper(), match.group(2).upper()


def format_consumable_level(level: ConsumableLevel) -> str:
    if level.level >= 0:
        text = f"{level.level}%"
        if level.status is ConsumableStatus.EMPTY:
            return f"{text}（已空）"

        return text

    return CONSUMABLE_STATUS_NAMES.get(level.status, "未知")


def format_waste_level(level: int, max_level: int) -> tuple[float, str]:
    if max_level <= 0:
        return 0, f"计数值：{level}（最大值未知）"

    percent = (level / max_level) * 100
    percent_display = max(0.0, min(percent, 100.0))
    return percent_display, f"{percent:0.1f}% ({level} / {max_level})"


def explain_exception(exc: Exception) -> str:
    if isinstance(exc, UnsupportedPrinterError):
        return str(exc)

    if isinstance(exc, OSError):
        return "无法访问 USB 打印设备。请确认设备已连接、打印机空闲，并尝试重新插拔 USB。"

    if isinstance(exc, AssertionError):
        return "打印机返回了无法识别的协议数据。请确认设备与当前协议兼容。"

    if isinstance(exc, KeyError):
        return f"打印机返回的设备标识不完整，缺少字段 {exc.args[0]!r}。"

    message = str(exc).strip()
    return message or exc.__class__.__name__


def format_error_dialog(exc: Exception) -> str:
    message = explain_exception(exc)
    raw_message = str(exc).strip()

    if raw_message and raw_message != message:
        message = f"{message}\n\n原始错误：{raw_message}"

    if LOG_PATH.exists():
        message = f"{message}\n\n详细日志：{LOG_PATH}"

    return message


def main() -> None:
    app = EzResetApp()
    app.mainloop()


if __name__ == "__main__":
    main()
