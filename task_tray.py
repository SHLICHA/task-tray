#!/usr/bin/env python3
"""Простой трекер задач в системном трее: напоминания, повторы, подзадачи."""
import calendar
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, QLocale, QObject, Signal, QPoint, QStandardPaths, Qt, QThread, QTime, QTimer, QUrl
from PySide6.QtGui import QAction, QBrush, QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket, QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QButtonGroup, QCheckBox, QComboBox, QDateEdit, QGridLayout, QWidgetAction, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton, QSpinBox, QSystemTrayIcon, QTimeEdit,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

APP_NAME = "Задачи"
VERSION = "1.0.0"                    # при релизе подставляется из git-тега автоматически
UPDATE_REPO = "SHLICHA/task-tray"    # репозиторий GitHub (owner/name), где публикуются релизы
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
ID_ROLE = Qt.UserRole
SUB_ROLE = Qt.UserRole + 1


# ───────────────────────────── логика повторов ─────────────────────────────

def parse_dt(s):
    return datetime.fromisoformat(s) if s else None


def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def hm(s):
    h, m = map(int, s.split(":"))
    return time(h, m)


def add_months(dt, n):
    m = dt.month - 1 + n
    y, m = dt.year + m // 12, m % 12 + 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def next_occurrence(rep, start, after):
    """Первое срабатывание строго позже `after` (но не раньше `start`)."""
    kind = rep.get("type", "none")
    n = max(1, int(rep.get("interval", 1)))
    after = max(after, start - timedelta(seconds=1))
    if kind == "none":
        return start if start > after else None
    if kind in ("minutes", "hours"):
        step = timedelta(minutes=n) if kind == "minutes" else timedelta(hours=n)
        days = set(rep.get("days") or range(7))
        win = rep.get("window")
        if not win and len(days) == 7:
            return start + ((after - start) // step + 1) * step
        ws, we = (hm(win[0]), hm(win[1])) if win else (time(0, 0), time(23, 59, 59))
        d = after.date()
        for _ in range(400):
            if d.weekday() in days:
                t = datetime.combine(d, ws)
                if t <= after:
                    t += ((after - t) // step + 1) * step
                if t <= datetime.combine(d, we):
                    return t
            d += timedelta(days=1)
        return None
    if kind == "days":
        step = timedelta(days=n)
        return start + ((after - start) // step + 1) * step
    if kind == "weekly":
        days = set(rep.get("days") or [start.weekday()])
        base = start.date() - timedelta(days=start.weekday())
        d = after.date()
        for _ in range(7 * n + 8):
            week = (d - timedelta(days=d.weekday()) - base).days // 7
            c = datetime.combine(d, start.time())
            if week % n == 0 and d.weekday() in days and c > after:
                return c
            d += timedelta(days=1)
        return None
    if kind == "monthly":
        k = max(0, ((after.year - start.year) * 12 + after.month - start.month) // n - 1)
        while True:
            c = add_months(start, k * n)
            if c > after:
                return c
            k += 1
    return None


def days_text(days):
    days = sorted(days)
    if len(days) == 7:
        return ""
    if days == [0, 1, 2, 3, 4]:
        return "пн–пт"
    return ", ".join(WEEKDAYS[d].lower() for d in days)


def describe_repeat(rep):
    kind, n = rep.get("type", "none"), int(rep.get("interval", 1))
    if kind == "none":
        return "—"
    if kind == "minutes":
        s = "каждую минуту" if n == 1 else f"каждые {n} мин"
    elif kind == "hours":
        s = "каждый час" if n == 1 else f"каждые {n} ч"
    elif kind == "days":
        return "каждый день" if n == 1 else f"каждые {n} дн."
    elif kind == "weekly":
        days = sorted(rep.get("days") or [])
        if days == [0, 1, 2, 3, 4] and n == 1:
            return "по будням"
        s = days_text(days) or "каждый день"
        return s + (f" (раз в {n} нед.)" if n > 1 else "")
    elif kind == "monthly":
        return "каждый месяц" if n == 1 else f"каждые {n} мес."
    else:
        return "?"
    extra = [days_text(rep.get("days") or range(7))]
    if rep.get("window"):
        extra.append(f"{rep['window'][0]}–{rep['window'][1]}")
    return " · ".join([s] + [e for e in extra if e])


def humanize(td):
    mins = max(0, round(td.total_seconds() / 60))
    if mins < 60:
        return f"{mins} мин"
    if mins < 1440:
        h, m = divmod(mins, 60)
        return f"{h} ч" + (f" {m} мин" if m else "")
    return f"{mins // 1440} дн."


def fmt_due(dt, now):
    if dt is None:
        return "без срока"
    d, today, t = dt.date(), now.date(), dt.strftime("%H:%M")
    if d == today:
        return f"Сегодня {t}"
    if d == today + timedelta(days=1):
        return f"Завтра {t}"
    if d == today - timedelta(days=1):
        return f"Вчера {t}"
    year = f".{d.year}" if d.year != today.year else ""
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}{year} {t}"


# ───────────────────────────── операции над задачами ─────────────────────────────

def new_task():
    return {
        "id": uuid.uuid4().hex, "title": "", "notes": "", "start": None, "due": None,
        "repeat": {"type": "none"}, "remind_before": 0, "nag": 0, "subtasks": [],
        "done": False, "done_at": None, "done_count": 0,
        "last_notified": None, "pre_notified": False, "snooze_until": None,
        "created": iso(datetime.now()),
    }


def is_recurring(task):
    return task["repeat"].get("type", "none") != "none"


def reset_flags(task):
    task["pre_notified"] = False
    task["snooze_until"] = None
    task["last_notified"] = None


def reschedule(task, now):
    """Пересчитать срок после создания/редактирования."""
    start = parse_dt(task["start"])
    if not start:
        task["due"] = None
    elif not is_recurring(task):
        task["due"] = task["start"]
    else:
        task["due"] = iso(next_occurrence(task["repeat"], start, now - timedelta(seconds=1)))
    reset_flags(task)


def complete(task, now):
    task["done_at"] = iso(now)
    task["done_count"] = task.get("done_count", 0) + 1
    if not is_recurring(task) or not task["due"]:
        task["done"] = True
        task["snooze_until"] = None
        return
    due = parse_dt(task["due"])
    nxt = next_occurrence(task["repeat"], parse_dt(task["start"]), max(now, due))
    task["due"] = iso(nxt)
    for s in task.get("subtasks", []):   # у повторяющейся задачи чек-лист начинается заново
        s["done"] = False
    reset_flags(task)
    if nxt is None:
        task["done"] = True


def reopen(task):
    task["done"] = False
    task["done_at"] = None


def roll_forward(task, now):
    """Пропущенные срабатывания повторяющейся задачи: переходим к последнему наступившему."""
    if task["done"] or not task["due"] or not is_recurring(task):
        return False
    rep, start, due = task["repeat"], parse_dt(task["start"]), parse_dt(task["due"])
    nxt, changed, guard = next_occurrence(rep, start, due), False, 0
    while nxt and nxt <= now and guard < 200000:
        due, nxt, changed, guard = nxt, next_occurrence(rep, start, nxt), True, guard + 1
    if changed:
        task["due"] = iso(due)
        task["pre_notified"] = False
        task["snooze_until"] = None
    return changed


def check_task(task, now):
    """Возвращает 'pre' / 'due', если пора оповестить, иначе None."""
    if task["done"] or not task["due"]:
        return None
    due = parse_dt(task["due"])
    snooze = parse_dt(task.get("snooze_until"))
    if snooze:
        if now < snooze:
            return None
        task["snooze_until"] = None
        task["last_notified"] = iso(now)
        return "due" if now >= due else "pre"
    if now >= due:
        last, nag = parse_dt(task.get("last_notified")), task.get("nag", 0)
        if last is None or last < due or (nag and now - last >= timedelta(minutes=nag)):
            task["last_notified"] = iso(now)
            return "due"
        return None
    rb = task.get("remind_before", 0)
    if rb and not task.get("pre_notified") and now >= due - timedelta(minutes=rb):
        task["pre_notified"] = True
        task["last_notified"] = iso(now)
        return "pre"
    return None


def sub_progress(task):
    subs = task.get("subtasks", [])
    return (sum(1 for s in subs if s["done"]), len(subs))


# ───────────────────────────── хранилище ─────────────────────────────

def data_dir():
    p = Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation) or Path.home() / ".task_tray")
    p.mkdir(parents=True, exist_ok=True)
    return p


class Store:
    def __init__(self, path):
        self.path = path
        self.tasks = []
        self.settings = {"system_notify": True, "popup": True, "sound": True, "paused": False,
                         "auto_update": True}
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
                self.tasks = data.get("tasks", [])
                self.settings.update(data.get("settings", {}))
                for t in self.tasks:
                    t.setdefault("subtasks", [])
            except Exception:
                path.replace(path.with_suffix(".broken.json"))

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tasks": self.tasks, "settings": self.settings},
                                  ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.path)

    def get(self, tid):
        return next((t for t in self.tasks if t["id"] == tid), None)


# ───────────────────────────── автозапуск ─────────────────────────────

AUTOSTART_NAME = "TaskTray"


def autostart_cmd():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --hidden'
    exe = Path(sys.executable)
    if os.name == "nt" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return f'"{exe}" "{Path(__file__).resolve()}" --hidden'


def autostart_supported():
    return os.name == "nt" or sys.platform.startswith("linux")


def _desktop_file():
    return Path.home() / ".config" / "autostart" / "task-tray.desktop"


def autostart_get():
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, AUTOSTART_NAME)
                return True
        except OSError:
            return False
    return _desktop_file().exists()


def autostart_set(on):
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_cmd())
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except OSError:
                    pass
        return
    f = _desktop_file()
    if on:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"[Desktop Entry]\nType=Application\nName={APP_NAME}\nExec={autostart_cmd()}\n"
                     "X-GNOME-Autostart-enabled=true\n", "utf-8")
    elif f.exists():
        f.unlink()


# ───────────────────────────── иконка ─────────────────────────────

def make_icon(alert=False):
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#2563eb"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    pen = QPen(QColor("white"), 7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.drawPolyline([QPoint(17, 33), QPoint(28, 44), QPoint(47, 21)])
    if alert:
        p.setPen(QPen(QColor("white"), 3))
        p.setBrush(QColor("#ef4444"))
        p.drawEllipse(38, 0, 26, 26)
    p.end()
    return QIcon(pm)


# ───────────────────────────── выбор времени ─────────────────────────────

class TimePicker(QWidget):
    """Поле времени ЧЧ:ММ: можно ввести с клавиатуры, крутить колесом или выбрать в сетке часов и минут."""
    timeChanged = Signal(QTime)
    PRESETS = ["09:00", "12:00", "15:00", "18:00", "20:00"]

    def __init__(self, t=QTime(9, 0), parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.edit = QTimeEdit(t)
        self.edit.setDisplayFormat("HH:mm")
        self.edit.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.edit.setAlignment(Qt.AlignCenter)
        self.edit.setFixedWidth(64)
        self.edit.setToolTip("Введите время или крутите колесо мыши над часами/минутами")
        self.edit.timeChanged.connect(self._sync)
        self.btn = QToolButton()
        self.btn.setText("🕒")
        self.btn.setToolTip("Выбрать время")
        self.btn.setPopupMode(QToolButton.InstantPopup)
        self.menu = QMenu(self.btn)
        act = QWidgetAction(self.menu)
        act.setDefaultWidget(self._build_grid())
        self.menu.addAction(act)
        self.menu.aboutToShow.connect(lambda: self._sync(self.edit.time()))
        self.btn.setMenu(self.menu)
        lay.addWidget(self.edit)
        lay.addWidget(self.btn)

    def _build_grid(self):
        w = QWidget()
        w.setStyleSheet("""
            QPushButton { min-width: 34px; max-width: 34px; min-height: 26px; padding: 0;
                          border: 1px solid transparent; border-radius: 5px; background: transparent; }
            QPushButton:hover { background: palette(midlight); }
            QPushButton:checked { background: #2563eb; color: white; }
            QPushButton[preset="true"] { min-width: 50px; max-width: 50px; border-color: palette(mid); }
            QLabel { color: gray; font-size: 11px; }
        """)
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(4)

        def grid(values, cols, on_click):
            g = QGridLayout()
            g.setSpacing(2)
            group = QButtonGroup(w)
            group.setExclusive(True)
            for i, val in enumerate(values):
                b = QPushButton(f"{val:02d}")
                b.setCheckable(True)
                b.setFocusPolicy(Qt.NoFocus)
                b.clicked.connect(lambda _=False, x=val: on_click(x))
                group.addButton(b, val)
                g.addWidget(b, i // cols, i % cols)
            return g, group

        v.addWidget(QLabel("Часы"))
        g, self.hours = grid(range(24), 6, self._pick_hour)
        v.addLayout(g)
        v.addWidget(QLabel("Минуты"))
        g, self.minutes = grid(range(0, 60, 5), 6, self._pick_minute)
        v.addLayout(g)
        v.addWidget(QLabel("Быстрый выбор"))
        row = QHBoxLayout()
        row.setSpacing(2)
        for text in self.PRESETS:
            b = QPushButton(text)
            b.setProperty("preset", True)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, x=text: self._pick_preset(x))
            row.addWidget(b)
        v.addLayout(row)
        return w

    def _sync(self, t):
        for group, val in ((self.hours, t.hour()), (self.minutes, t.minute())):
            b = group.button(val)
            if b:
                b.setChecked(True)
            elif group.checkedButton():          # минута не кратна 5 — снять выделение
                group.setExclusive(False)
                group.checkedButton().setChecked(False)
                group.setExclusive(True)
        self.timeChanged.emit(t)

    def _pick_hour(self, h):
        self.edit.setTime(QTime(h, self.edit.time().minute()))   # меню остаётся открытым: дальше минуты

    def _pick_minute(self, m):
        self.edit.setTime(QTime(self.edit.time().hour(), m))
        self.menu.hide()

    def _pick_preset(self, text):
        self.edit.setTime(QTime.fromString(text, "HH:mm"))
        self.menu.hide()

    def time(self):
        return self.edit.time()

    def setTime(self, t):
        self.edit.setTime(t)



# ───────────────────────────── диалог задачи ─────────────────────────────

class TaskDialog(QDialog):
    REPEATS = [("none", "Не повторять"), ("minutes", "Каждые N минут"), ("hours", "Каждые N часов"),
               ("days", "Каждые N дней"), ("workdays", "По будням"), ("weekly", "По дням недели"),
               ("monthly", "Каждые N месяцев")]
    UNITS = {"minutes": "мин", "hours": "ч", "days": "дн.", "weekly": "нед.", "monthly": "мес."}
    BEFORE = [(0, "В момент срока"), (5, "За 5 минут"), (10, "За 10 минут"), (15, "За 15 минут"),
              (30, "За 30 минут"), (60, "За 1 час"), (120, "За 2 часа"), (1440, "За 1 день")]
    NAG = [(0, "Нет"), (5, "Каждые 5 минут"), (10, "Каждые 10 минут"), (15, "Каждые 15 минут"),
           (30, "Каждые 30 минут"), (60, "Каждый час")]

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task
        self.setWindowTitle("Новая задача" if not task["title"] else "Задача")
        self.setMinimumWidth(600)
        form = self.form = QFormLayout(self)

        self.title = QLineEdit(task["title"])
        self.title.setPlaceholderText("Что нужно сделать")
        form.addRow("Название:", self.title)

        self.notes = QPlainTextEdit(task.get("notes", ""))
        self.notes.setFixedHeight(54)
        form.addRow("Заметка:", self.notes)

        # подзадачи
        subw = QWidget()
        sv = QVBoxLayout(subw)
        sv.setContentsMargins(0, 0, 0, 0)
        self.subs = QListWidget()
        self.subs.setFixedHeight(110)
        self.subs.setDragDropMode(QListWidget.InternalMove)
        for s in task.get("subtasks", []):
            self._add_sub_item(s["title"], s["done"], s["id"])
        row = QHBoxLayout()
        self.sub_input = QLineEdit()
        self.sub_input.setPlaceholderText("Новая подзадача — Enter, чтобы добавить")
        self.sub_input.returnPressed.connect(self._add_sub)
        b_add, b_del = QPushButton("＋"), QPushButton("−")
        for b in (b_add, b_del):
            b.setFixedWidth(32)
            b.setAutoDefault(False)
        b_add.clicked.connect(self._add_sub)
        b_del.clicked.connect(lambda: [self.subs.takeItem(self.subs.row(i)) for i in self.subs.selectedItems()])
        row.addWidget(self.sub_input)
        row.addWidget(b_add)
        row.addWidget(b_del)
        sv.addWidget(self.subs)
        sv.addLayout(row)
        form.addRow("Подзадачи:", subw)

        # срок
        start = parse_dt(task["start"]) or (datetime.now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        self.has_due = QCheckBox()
        self.has_due.setChecked(bool(task["start"]) or not task["title"])
        self.date = QDateEdit(QDate(start.year, start.month, start.day))
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("dd.MM.yyyy, ddd")
        self.date.calendarWidget().setFirstDayOfWeek(Qt.Monday)
        self.date.calendarWidget().setGridVisible(True)
        dw = QWidget()
        dl = QHBoxLayout(dw)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.has_due)
        dl.addWidget(self.date, 1)
        self.quick_dates = []
        for label, days in (("Сегодня", 0), ("Завтра", 1), ("+неделя", 7)):
            b = QToolButton()
            b.setText(label)
            b.clicked.connect(lambda _=False, d=days: self.date.setDate(QDate.currentDate().addDays(d)))
            self.quick_dates.append(b)
            dl.addWidget(b)
        form.addRow("Дата:", dw)
        self.due_row = dw

        self.time = TimePicker(QTime(start.hour, start.minute))
        tw = QWidget()
        tl = QHBoxLayout(tw)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.time)
        tl.addStretch()
        form.addRow("Время:", tw)
        self.time_row = tw

        # повтор
        rep = task["repeat"]
        self.repeat = QComboBox()
        for k, label in self.REPEATS:
            self.repeat.addItem(label, k)
        kind = rep.get("type", "none")
        if kind == "weekly" and sorted(rep.get("days") or []) == [0, 1, 2, 3, 4] and rep.get("interval", 1) == 1:
            kind = "workdays"
        self.repeat.setCurrentIndex(self.repeat.findData(kind))
        self.interval = QSpinBox()
        self.interval.setRange(1, 999)
        self.interval.setValue(int(rep.get("interval", 1)))
        self.unit = QLabel()
        rw = QWidget()
        rl = QHBoxLayout(rw)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.repeat, 1)
        self.every = QLabel("N =")
        rl.addWidget(self.every)
        rl.addWidget(self.interval)
        rl.addWidget(self.unit)
        form.addRow("Повтор:", rw)

        self.day_cbs = []
        days = set(rep.get("days") or range(7))
        dw2 = QWidget()
        dl2 = QHBoxLayout(dw2)
        dl2.setContentsMargins(0, 0, 0, 0)
        for i, name in enumerate(WEEKDAYS):
            cb = QCheckBox(name)
            cb.setChecked(i in days)
            cb.toggled.connect(self._update)
            self.day_cbs.append(cb)
            dl2.addWidget(cb)
        for label, sel in (("будни", range(5)), ("все", range(7))):
            b = QToolButton()
            b.setText(label)
            b.clicked.connect(lambda _=False, s=set(sel): [c.setChecked(i in s) for i, c in enumerate(self.day_cbs)])
            dl2.addWidget(b)
        dl2.addStretch()
        form.addRow("Дни:", dw2)
        self.days_row = dw2

        win = rep.get("window")
        self.win_cb = QCheckBox("Только с")
        self.win_cb.setChecked(bool(win))
        self.win_from = TimePicker(QTime.fromString(win[0] if win else "09:00", "HH:mm"))
        self.win_to = TimePicker(QTime.fromString(win[1] if win else "18:00", "HH:mm"))
        ww = QWidget()
        wl = QHBoxLayout(ww)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(self.win_cb)
        wl.addWidget(self.win_from)
        wl.addWidget(QLabel("до"))
        wl.addWidget(self.win_to)
        wl.addStretch()
        form.addRow("Активно:", ww)
        self.win_row = ww

        self.before = self._combo(self.BEFORE, task.get("remind_before", 0), "За {} мин")
        form.addRow("Напомнить:", self.before)
        self.nag = self._combo(self.NAG, task.get("nag", 0), "Каждые {} мин")
        form.addRow("Повторять, пока\nне выполнено:", self.nag)

        self.preview = QLabel()
        self.preview.setStyleSheet("color: gray")
        self.preview.setWordWrap(True)
        form.addRow("", self.preview)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("Сохранить")
        bb.button(QDialogButtonBox.Cancel).setText("Отмена")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

        for sig in (self.has_due.toggled, self.date.dateChanged, self.time.timeChanged, self.repeat.currentIndexChanged,
                    self.interval.valueChanged, self.win_cb.toggled, self.win_from.timeChanged,
                    self.win_to.timeChanged):
            sig.connect(self._update)
        self._update()
        self.title.setFocus()

    def _combo(self, items, value, custom):
        c = QComboBox()
        for v, label in items:
            c.addItem(label, v)
        if c.findData(value) < 0:
            c.addItem(custom.format(value), value)
        c.setCurrentIndex(c.findData(value))
        return c

    def _add_sub_item(self, title, done=False, sid=None):
        it = QListWidgetItem(title)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
        it.setCheckState(Qt.Checked if done else Qt.Unchecked)
        it.setData(SUB_ROLE, sid or uuid.uuid4().hex)
        self.subs.addItem(it)

    def _add_sub(self):
        text = self.sub_input.text().strip()
        if text:
            self._add_sub_item(text)
            self.sub_input.clear()

    def _rep(self):
        kind = self.repeat.currentData()
        if kind == "workdays":
            return {"type": "weekly", "interval": 1, "days": [0, 1, 2, 3, 4]}
        rep = {"type": kind, "interval": self.interval.value()}
        days = [i for i, c in enumerate(self.day_cbs) if c.isChecked()]
        if kind == "weekly" or (kind in ("minutes", "hours") and len(days) < 7):
            rep["days"] = days
        if kind in ("minutes", "hours") and self.win_cb.isChecked():
            rep["window"] = [self.win_from.time().toString("HH:mm"), self.win_to.time().toString("HH:mm")]
        return rep

    def _start(self):
        d, t = self.date.date(), self.time.time()
        return datetime(d.year(), d.month(), d.day(), t.hour(), t.minute())

    def _update(self):
        kind = self.repeat.currentData()
        recurring = kind != "none"
        if recurring:
            self.has_due.setChecked(True)
        self.has_due.setEnabled(not recurring)
        on = self.has_due.isChecked()
        for w in [self.date, self.time] + self.quick_dates:
            w.setEnabled(on)
        self.form.labelForField(self.due_row).setText("Начиная с:" if recurring else "Дата:")
        self.form.labelForField(self.time_row).setText("Первый раз в:" if kind in ("minutes", "hours") else "Время:")
        show_n = kind in self.UNITS
        for w in (self.every, self.interval, self.unit):
            w.setVisible(show_n)
        self.unit.setText(self.UNITS.get(kind, ""))
        self.form.setRowVisible(self.days_row, kind in ("minutes", "hours", "weekly"))
        self.form.setRowVisible(self.win_row, kind in ("minutes", "hours"))
        self.win_from.setEnabled(self.win_cb.isChecked())
        self.win_to.setEnabled(self.win_cb.isChecked())
        for w in (self.before, self.nag):
            w.setEnabled(self.has_due.isChecked())
        # предпросмотр ближайших срабатываний
        if not self.has_due.isChecked():
            self.preview.setText("Задача без срока — без оповещений.")
            return
        if not recurring:
            self.preview.setText("")
            return
        now, start, rep, out = datetime.now(), self._start(), self._rep(), []
        t = now - timedelta(seconds=1)
        for _ in range(4):
            t = next_occurrence(rep, start, t)
            if not t:
                break
            out.append(fmt_due(t, now))
        self.preview.setText("Ближайшие: " + ", ".join(out) if out else "Нет подходящего времени — проверьте дни и интервал.")
        self.adjustSize()

    def _accept(self):
        if not self.title.text().strip():
            self.title.setFocus()
            return
        rep = self._rep()
        if rep.get("days") == []:
            QMessageBox.warning(self, APP_NAME, "Выберите хотя бы один день недели.")
            return
        if rep.get("window") and self.win_from.time() >= self.win_to.time():
            QMessageBox.warning(self, APP_NAME, "Начало интервала должно быть раньше конца.")
            return
        self._add_sub()
        t = self.task
        t["title"] = self.title.text().strip()
        t["notes"] = self.notes.toPlainText().strip()
        t["start"] = iso(self._start()) if self.has_due.isChecked() else None
        t["repeat"] = rep
        t["remind_before"] = self.before.currentData()
        t["nag"] = self.nag.currentData()
        t["subtasks"] = [{"id": it.data(SUB_ROLE), "title": it.text().strip(), "done": it.checkState() == Qt.Checked}
                         for it in (self.subs.item(i) for i in range(self.subs.count())) if it.text().strip()]
        self.accept()


# ───────────────────────────── всплывающее напоминание ─────────────────────────────

class ReminderPopup(QFrame):
    def __init__(self, ctl, tid):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.ctl, self.tid = ctl, tid
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedWidth(380)
        self.setObjectName("popup")
        self.setStyleSheet("""
            #popup { background: #1f2937; border: 1px solid #374151; border-radius: 10px; }
            QLabel { color: #f9fafb; }
            QLabel#head { color: #93c5fd; font-size: 11px; }
            QLabel#sub { color: #d1d5db; font-size: 11px; }
            QPushButton, QToolButton { background: #374151; color: white; border: none;
                                       border-radius: 6px; padding: 5px 10px; }
            QPushButton:hover, QToolButton:hover { background: #4b5563; }
            QPushButton#done { background: #2563eb; }
            QPushButton#done:hover { background: #1d4ed8; }
        """)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        top = QHBoxLayout()
        self.head = QLabel()
        self.head.setObjectName("head")
        close = QToolButton()
        close.setText("✕")
        close.clicked.connect(lambda: ctl.close_popup(tid))
        top.addWidget(self.head, 1)
        top.addWidget(close)
        v.addLayout(top)
        self.title = QLabel()
        self.title.setWordWrap(True)
        f = self.title.font()
        f.setPointSize(f.pointSize() + 2)
        f.setBold(True)
        self.title.setFont(f)
        v.addWidget(self.title)
        self.sub = QLabel()
        self.sub.setObjectName("sub")
        self.sub.setWordWrap(True)
        v.addWidget(self.sub)
        row = QHBoxLayout()
        done = QPushButton("✓ Выполнено")
        done.setObjectName("done")
        done.clicked.connect(lambda: ctl.complete(tid))
        snooze = QToolButton()
        snooze.setText("Отложить ▾")
        snooze.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(snooze)
        for mins, label in ((5, "на 5 минут"), (10, "на 10 минут"), (30, "на 30 минут"), (60, "на 1 час"), (180, "на 3 часа")):
            m.addAction(label, lambda mm=mins: ctl.snooze(tid, mm))
        snooze.setMenu(m)
        openb = QPushButton("Открыть")
        openb.clicked.connect(lambda: ctl.edit_task(tid))
        row.addWidget(done)
        row.addWidget(snooze)
        row.addStretch()
        row.addWidget(openb)
        v.addLayout(row)

    def set_content(self, task, head):
        self.head.setText(head)
        self.title.setText(task["title"])
        lines = []
        d, n = sub_progress(task)
        if n:
            lines.append(f"Подзадачи: {d}/{n}")
            lines += [f"  ☐ {s['title']}" for s in task["subtasks"] if not s["done"]][:5]
        if task.get("notes"):
            lines.append(task["notes"][:200])
        self.sub.setText("\n".join(lines))
        self.sub.setVisible(bool(lines))
        self.adjustSize()


# ───────────────────────────── обновления ─────────────────────────────

def parse_version(s):
    parts = []
    for p in (s or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def pick_asset(assets):
    """Какой файл релиза нужен: .exe для собранной Windows-версии, task_tray.py для запуска из исходника."""
    frozen = getattr(sys, "frozen", False)
    for a in assets:
        name = a.get("name", "").lower()
        if frozen and os.name == "nt" and name.endswith(".exe"):
            return a
        if not frozen and name == "task_tray.py":
            return a
    return None


def windows_update_script(new_exe, cur_exe):
    """bat-файл: ждёт закрытия программы, подменяет exe и запускает новую версию."""
    return (
        "@echo off\r\nchcp 65001 >nul\r\n"
        "set n=0\r\n"
        ":wait\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        f'move /y "{new_exe}" "{cur_exe}" >nul 2>&1 && goto run\r\n'
        "set /a n+=1\r\nif %n% lss 60 goto wait\r\nexit /b 1\r\n"
        ":run\r\n"
        f'start "" "{cur_exe}" --after-update\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )


class Updater(QObject):
    CHECK_EVERY_MS = 6 * 3600 * 1000

    def __init__(self, ctl):
        super().__init__(ctl)
        self.ctl = ctl
        self.nam = QNetworkAccessManager(self)
        self.busy = False
        self.available = None          # (tag, release_json, asset)
        self.timer = QTimer(self)
        self.timer.setInterval(self.CHECK_EVERY_MS)
        self.timer.timeout.connect(self.auto_check)
        self.timer.start()
        QTimer.singleShot(20000, self.auto_check)

    def _request(self, url, accept):
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"Accept", accept)
        req.setRawHeader(b"User-Agent", b"TaskTray")
        return req

    def auto_check(self):
        if self.ctl.store.settings.get("auto_update", True):
            self.check(silent=True)

    def check(self, silent=False):
        if self.busy:
            return
        self.busy = True
        r = self.nam.get(self._request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                       b"application/vnd.github+json"))
        r.finished.connect(lambda: self._on_info(r, silent))

    def _say(self, silent, text, warn=False):
        if not silent:
            (QMessageBox.warning if warn else QMessageBox.information)(self.ctl.window, APP_NAME, text)

    def _on_info(self, r, silent):
        self.busy = False
        r.deleteLater()
        if r.error() != QNetworkReply.NoError:
            return self._say(silent, f"Не удалось проверить обновления:\n{r.errorString()}", True)
        try:
            data = json.loads(bytes(r.readAll()).decode("utf-8"))
        except ValueError:
            return self._say(silent, "Сервер обновлений вернул непонятный ответ.", True)
        tag = data.get("tag_name", "")
        if parse_version(tag) <= parse_version(VERSION):
            return self._say(silent, f"У вас последняя версия ({VERSION}).")
        asset = pick_asset(data.get("assets", []))
        if not asset:
            return self._say(silent, f"Вышла версия {tag}, но для этой системы нет готового файла.\n"
                                     f"Скачайте её вручную: {data.get('html_url', '')}", True)
        first_time = not self.available or self.available[0] != tag
        self.available = (tag, data, asset)
        self.ctl.update_tray()
        if not silent:
            self.offer()
        elif first_time:
            self.ctl.tray.showMessage(APP_NAME, f"Доступна новая версия {tag}. Обновить — через меню значка.",
                                      QSystemTrayIcon.Information, 8000)

    def offer(self):
        if not self.available or self.busy:
            return
        tag, data, asset = self.available
        notes = (data.get("body") or "").strip()
        box = QMessageBox(QMessageBox.Question, APP_NAME,
                          f"Доступна версия {tag} (у вас {VERSION}).\n\nОбновить сейчас? Программа перезапустится.",
                          QMessageBox.Yes | QMessageBox.No, self.ctl.window if self.ctl.window.isVisible() else None)
        box.button(QMessageBox.Yes).setText("Обновить")
        box.button(QMessageBox.No).setText("Позже")
        if notes:
            box.setDetailedText(notes[:4000])
        if box.exec() == QMessageBox.Yes:
            self.download(asset)

    def download(self, asset):
        self.busy = True
        self.progress = QProgressDialog("Загрузка обновления…", "Отмена", 0, 100)
        self.progress.setWindowTitle(APP_NAME)
        self.progress.setMinimumDuration(0)
        r = self.nam.get(self._request(asset["browser_download_url"], b"application/octet-stream"))
        r.downloadProgress.connect(lambda got, total: total > 0 and self.progress.setValue(int(got * 100 / total)))
        self.progress.canceled.connect(r.abort)
        r.finished.connect(lambda: self._on_file(r, asset["name"]))

    def _on_file(self, r, name):
        self.busy = False
        self.progress.close()
        r.deleteLater()
        if r.error() != QNetworkReply.NoError:
            if r.error() != QNetworkReply.OperationCanceledError:
                self._say(False, f"Не удалось скачать обновление:\n{r.errorString()}", True)
            return
        payload = bytes(r.readAll())
        try:
            self.apply(payload, name)
        except Exception as e:  # noqa: BLE001
            self._say(False, f"Не удалось установить обновление:\n{e}", True)

    def apply(self, payload, name):
        self.ctl.store.save()
        tmp = Path(tempfile.gettempdir())
        if getattr(sys, "frozen", False):
            cur = Path(sys.executable)
            if os.name == "nt" and not payload.startswith(b"MZ"):
                raise ValueError("скачанный файл не похож на программу Windows")
            new = tmp / f"Tasks-update-{os.getpid()}{cur.suffix}"
            new.write_bytes(payload)
            if os.name == "nt":
                bat = tmp / f"tasks-update-{os.getpid()}.bat"
                bat.write_text(windows_update_script(new, cur), encoding="utf-8")
                subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x08000000)  # CREATE_NO_WINDOW
            else:
                os.replace(new, cur)
                cur.chmod(0o755)
                subprocess.Popen([str(cur), "--after-update"])
        else:
            compile(payload, name, "exec")                     # проверка, что файл целый
            script = Path(__file__).resolve()
            script.with_suffix(".py.bak").write_bytes(script.read_bytes())
            new = script.with_name(script.name + ".new")
            new.write_bytes(payload)
            os.replace(new, script)
            subprocess.Popen([sys.executable, str(script), "--after-update"])
        QApplication.quit()


# ───────────────────────────── главное окно ─────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self._filling = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(ctl.icon_ok)
        self.resize(960, 500)
        w = QWidget()
        self.setCentralWidget(w)
        v = QVBoxLayout(w)

        bar = QHBoxLayout()
        b_add = QPushButton("＋ Новая задача")
        b_add.clicked.connect(ctl.add_task)
        self.b_done = QPushButton("✓ Выполнено")
        self.b_done.clicked.connect(lambda: self._with_sel(ctl.complete))
        b_edit = QPushButton("Изменить")
        b_edit.clicked.connect(lambda: self._with_sel(ctl.edit_task))
        b_del = QPushButton("Удалить")
        b_del.clicked.connect(lambda: self._with_sel(ctl.delete_task))
        self.filter = QComboBox()
        self.filter.addItems(["Активные", "Выполненные", "Все"])
        self.filter.currentIndexChanged.connect(self.refresh)
        settings = QToolButton()
        settings.setText("⚙")
        settings.setPopupMode(QToolButton.InstantPopup)
        settings.setMenu(ctl.settings_menu(self))
        for b in (b_add, self.b_done, b_edit, b_del):
            bar.addWidget(b)
        bar.addStretch()
        bar.addWidget(self.filter)
        bar.addWidget(settings)
        v.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Задача", "Когда", "Повтор", "Напоминание"])
        self.tree.setAlternatingRowColors(True)
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in (1, 2, 3):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.tree.itemDoubleClicked.connect(self._double)
        self.tree.itemChanged.connect(self._changed)
        self.collapsed = set()
        self.tree.itemCollapsed.connect(lambda it: self._filling or self.collapsed.add(it.data(0, ID_ROLE)))
        self.tree.itemExpanded.connect(lambda it: self._filling or self.collapsed.discard(it.data(0, ID_ROLE)))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context)
        v.addWidget(self.tree)
        self.status = QLabel()
        self.status.setStyleSheet("color: gray")
        v.addWidget(self.status)
        self._hinted = False

    def selected(self):
        it = self.tree.currentItem()
        if not it:
            return None, None
        return it.data(0, ID_ROLE), it.data(0, SUB_ROLE)

    def _with_sel(self, fn):
        tid, _ = self.selected()
        if tid:
            fn(tid)

    def _double(self, it, _col):
        self.ctl.edit_task(it.data(0, ID_ROLE))

    def _changed(self, it, col):
        if self._filling or col != 0:
            return
        tid, sid = it.data(0, ID_ROLE), it.data(0, SUB_ROLE)
        checked = it.checkState(0) == Qt.Checked
        if sid:
            QTimer.singleShot(0, lambda: self.ctl.toggle_sub(tid, sid, checked))
        elif checked:
            QTimer.singleShot(0, lambda: self.ctl.complete(tid))
        else:
            QTimer.singleShot(0, lambda: self.ctl.reopen(tid))

    def _context(self, pos):
        it = self.tree.itemAt(pos)
        if not it:
            return
        tid = it.data(0, ID_ROLE)
        task = self.ctl.store.get(tid)
        m = QMenu(self)
        if task["done"]:
            m.addAction("Вернуть в работу", lambda: self.ctl.reopen(tid))
        else:
            m.addAction("✓ Выполнено", lambda: self.ctl.complete(tid))
            if task["due"]:
                sm = m.addMenu("Отложить")
                for mins, label in ((10, "на 10 минут"), (60, "на 1 час"), (180, "на 3 часа")):
                    sm.addAction(label, lambda mm=mins: self.ctl.snooze(tid, mm))
        m.addAction("Изменить…", lambda: self.ctl.edit_task(tid))
        m.addSeparator()
        m.addAction("Удалить", lambda: self.ctl.delete_task(tid))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def refresh(self):
        sel_tid, sel_sid = self.selected()
        self._filling = True
        self.tree.clear()
        now = datetime.now()
        mode = self.filter.currentIndex()
        tasks = [t for t in self.ctl.store.tasks if mode == 2 or (mode == 0) == (not t["done"])]
        if mode == 1:
            tasks.sort(key=lambda t: t["done_at"] or "", reverse=True)
        else:
            tasks.sort(key=lambda t: (t["done"], t["due"] is None, t["due"] or "", t["title"].lower()))
        red, gray = QBrush(QColor("#dc2626")), QBrush(QColor("#9ca3af"))
        overdue_n = 0
        for t in tasks:
            due = parse_dt(t["due"])
            overdue = not t["done"] and due is not None and due <= now
            overdue_n += overdue
            d, n = sub_progress(t)
            title = t["title"] + (f"   [{d}/{n}]" if n else "")
            if t["done"]:
                when = "выполнено " + fmt_due(parse_dt(t["done_at"]), now) if t["done_at"] else "выполнено"
            else:
                when = fmt_due(due, now)
                snooze = parse_dt(t.get("snooze_until"))
                if snooze and snooze > now:
                    when += f"  (отложено до {snooze:%H:%M})"
            remind = []
            if t["due"] or t["start"]:
                rb = t.get("remind_before", 0)
                remind.append(f"за {humanize(timedelta(minutes=rb))}" if rb else "в срок")
                if t.get("nag"):
                    remind.append(f"повтор {t['nag']} мин")
            it = QTreeWidgetItem([title, when, describe_repeat(t["repeat"]), " · ".join(remind)])
            it.setData(0, ID_ROLE, t["id"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked if t["done"] else Qt.Unchecked)
            if t.get("notes"):
                it.setToolTip(0, t["notes"])
            if overdue:
                it.setForeground(1, red)
                f = it.font(1)
                f.setBold(True)
                it.setFont(1, f)
            if t["done"]:
                for c in range(4):
                    it.setForeground(c, gray)
            self.tree.addTopLevelItem(it)
            for s in t.get("subtasks", []):
                ch = QTreeWidgetItem([s["title"]])
                ch.setData(0, ID_ROLE, t["id"])
                ch.setData(0, SUB_ROLE, s["id"])
                ch.setFlags(ch.flags() | Qt.ItemIsUserCheckable)
                ch.setCheckState(0, Qt.Checked if s["done"] else Qt.Unchecked)
                if s["done"] or t["done"]:
                    ch.setForeground(0, gray)
                it.addChild(ch)
                if sel_sid == s["id"]:
                    self.tree.setCurrentItem(ch)
            it.setExpanded(not t["done"] and t["id"] not in self.collapsed)
            if sel_tid == t["id"] and not sel_sid:
                self.tree.setCurrentItem(it)
        self._filling = False
        active = sum(1 for t in self.ctl.store.tasks if not t["done"])
        self.status.setText(f"Активных: {active}" + (f"   ·   просрочено: {overdue_n}" if overdue_n else "")
                            + ("   ·   оповещения на паузе" if self.ctl.store.settings["paused"] else ""))

    def closeEvent(self, e):
        e.ignore()
        self.hide()
        if not self._hinted and self.ctl.tray.isVisible():
            self._hinted = True
            self.ctl.tray.showMessage(APP_NAME, "Работаю в трее. Выход — через меню иконки.",
                                      QSystemTrayIcon.Information, 3000)


# ───────────────────────────── контроллер ─────────────────────────────

class Controller(QObject):
    def __init__(self, hidden=False):
        super().__init__()
        self.store = Store(data_dir() / "tasks.json")
        self.icon_ok, self.icon_alert = make_icon(False), make_icon(True)
        self.popups = {}
        self.dialog_open = set()

        self.tray = QSystemTrayIcon(self.icon_ok)
        self.tray_menu = QMenu()
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self.show_window)
        self.tray.show()

        self.updater = Updater(self)
        self.window = MainWindow(self)
        self.window.setWindowTitle(f"{APP_NAME} {VERSION}")
        self.window.refresh()
        self.update_tray()

        self.timer = QTimer(self)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        QTimer.singleShot(1500, self.tick)
        if not hidden or not QSystemTrayIcon.isSystemTrayAvailable():
            self.show_window()

    # — настройки —
    def settings_menu(self, parent):
        m = QMenu(parent)
        s = self.store.settings
        for key, label in (("system_notify", "Системные уведомления"), ("popup", "Окно с кнопками"),
                           ("sound", "Звук"), ("paused", "Пауза оповещений")):
            a = QAction(label, m, checkable=True, checked=s[key])
            a.toggled.connect(lambda on, k=key: self._set(k, on))
            m.addAction(a)
        if autostart_supported():
            m.addSeparator()
            a = QAction("Запускать при входе в систему", m, checkable=True, checked=autostart_get())
            a.toggled.connect(autostart_set)
            m.addAction(a)
        m.addSeparator()
        a = QAction("Проверять обновления автоматически", m, checkable=True,
                    checked=self.store.settings.get("auto_update", True))
        a.toggled.connect(lambda on: self._set("auto_update", on))
        m.addAction(a)
        m.addAction(f"Проверить обновления (версия {VERSION})", lambda: self.updater.check(silent=False))
        m.addAction("Папка с данными", lambda: QMessageBox.information(
            self.window, APP_NAME, f"Задачи хранятся в файле:\n{self.store.path}"))
        return m

    def _set(self, key, on):
        self.store.settings[key] = on
        self.store.save()
        self.window.refresh()
        self.update_tray()

    # — окно/трей —
    def show_window(self):
        self.window.refresh()
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.window.isVisible() and not self.window.isMinimized():
                self.window.hide()
            else:
                self.show_window()

    def update_tray(self):
        now = datetime.now()
        active = [t for t in self.store.tasks if not t["done"] and t["due"]]
        active.sort(key=lambda t: t["due"])
        overdue = [t for t in active if parse_dt(t["due"]) <= now]
        self.tray.setIcon(self.icon_alert if overdue else self.icon_ok)
        tip = APP_NAME
        if overdue:
            tip += f" — просрочено: {len(overdue)}"
        upcoming = [t for t in active if parse_dt(t["due"]) > now]
        if upcoming:
            tip += f"\nДальше: {upcoming[0]['title']} — {fmt_due(parse_dt(upcoming[0]['due']), now)}"
        self.tray.setToolTip(tip)

        m = self.tray_menu
        m.clear()
        m.addAction("Открыть", self.show_window)
        m.addAction("Новая задача…", self.add_task)
        end_of_day = datetime.combine(now.date(), time(23, 59, 59))
        today = [t for t in active if parse_dt(t["due"]) <= end_of_day][:8]
        if today:
            m.addSeparator()
            m.addSection("Отметить выполненной")
            for t in today:
                due = parse_dt(t["due"])
                mark = "⚠ " if due <= now else ""
                m.addAction(f"{mark}{due:%H:%M}  {t['title'][:40]}", lambda tid=t["id"]: self.complete(tid))
        m.addSeparator()
        if self.updater.available:
            m.addAction(f"⬆ Обновить до {self.updater.available[0]}", self.updater.offer)
        a = QAction("Пауза оповещений", m, checkable=True, checked=self.store.settings["paused"])
        a.toggled.connect(lambda on: self._set("paused", on))
        m.addAction(a)
        m.addAction("Выход", QApplication.quit)

    def changed(self):
        self.store.save()
        self.window.refresh()
        self.update_tray()

    # — таймер —
    def tick(self):
        now = datetime.now().replace(microsecond=0)
        changed = False
        for t in self.store.tasks:
            changed |= roll_forward(t, now)
            ev = check_task(t, now)
            if ev:
                changed = True
                if not self.store.settings["paused"]:
                    self.notify(t, ev, now)
        if changed:
            self.store.save()
        if changed or self.window.isVisible():
            self.window.refresh()
        self.update_tray()

    def notify(self, task, kind, now):
        due = parse_dt(task["due"])
        if kind == "pre":
            head = f"Через {humanize(due - now)} · {due:%H:%M}"
        elif now - due < timedelta(minutes=2):
            head = f"Пора! · {due:%H:%M}"
        else:
            head = f"Просрочено · {fmt_due(due, now)}"
        s = self.store.settings
        if s["system_notify"]:
            self.tray.showMessage(head, task["title"], QSystemTrayIcon.Information, 10000)
        if s["popup"]:
            p = self.popups.get(task["id"])
            if not p:
                p = self.popups[task["id"]] = ReminderPopup(self, task["id"])
            p.set_content(task, head)
            p.show()
            p.raise_()
            self.layout_popups()
        if s["sound"]:
            QApplication.beep()

    def layout_popups(self):
        geo = QGuiApplication.primaryScreen().availableGeometry()
        y = geo.bottom() - 12
        for p in self.popups.values():
            p.adjustSize()
            p.move(geo.right() - p.width() - 12, y - p.height())
            y -= p.height() + 8

    def close_popup(self, tid):
        p = self.popups.pop(tid, None)
        if p:
            p.close()
            p.deleteLater()
            self.layout_popups()

    # — действия —
    def _dialog(self, task, title_new):
        dlg = TaskDialog(task, self.window if self.window.isVisible() else None)
        if title_new:
            dlg.setWindowTitle("Новая задача")
        dlg.setWindowIcon(self.icon_ok)
        return dlg.exec() == QDialog.Accepted

    def add_task(self):
        t = new_task()
        if self._dialog(t, True):
            reschedule(t, datetime.now())
            self.store.tasks.append(t)
            self.changed()

    def edit_task(self, tid):
        task = self.store.get(tid)
        if not task or tid in self.dialog_open:
            return
        self.dialog_open.add(tid)
        try:
            copy = json.loads(json.dumps(task))
            old = (task["start"], task["repeat"])
            if self._dialog(copy, False):
                task.update(copy)
                if (task["start"], task["repeat"]) != old:
                    reschedule(task, datetime.now())
                self.close_popup(tid)
                self.changed()
        finally:
            self.dialog_open.discard(tid)

    def delete_task(self, tid):
        task = self.store.get(tid)
        if task and QMessageBox.question(self.window, APP_NAME, f"Удалить «{task['title']}»?") == QMessageBox.Yes:
            self.store.tasks.remove(task)
            self.close_popup(tid)
            self.changed()

    def complete(self, tid):
        task = self.store.get(tid)
        if not task:
            return
        if not task["done"]:
            complete(task, datetime.now().replace(microsecond=0))
        self.close_popup(tid)
        self.changed()

    def reopen(self, tid):
        task = self.store.get(tid)
        if task:
            reopen(task)
            self.changed()

    def toggle_sub(self, tid, sid, done):
        task = self.store.get(tid)
        if not task:
            return
        for s in task["subtasks"]:
            if s["id"] == sid:
                s["done"] = done
        self.changed()
        if tid in self.popups:
            self.popups[tid].set_content(task, self.popups[tid].head.text())
        d, n = sub_progress(task)
        if done and n and d == n and not task["done"]:
            if QMessageBox.question(self.window, APP_NAME,
                                    f"Все подзадачи выполнены. Отметить «{task['title']}» выполненной?") == QMessageBox.Yes:
                self.complete(tid)

    def snooze(self, tid, minutes):
        task = self.store.get(tid)
        if task:
            task["snooze_until"] = iso(datetime.now().replace(microsecond=0) + timedelta(minutes=minutes))
            self.close_popup(tid)
            self.changed()


# ───────────────────────────── запуск ─────────────────────────────

def main():
    app = QApplication(sys.argv)
    QLocale.setDefault(QLocale(QLocale.Russian, QLocale.Russia))   # русские дни недели в календаре
    app.setApplicationName("TaskTray")
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    server_name = "task-tray-" + (os.environ.get("USERNAME") or os.environ.get("USER") or "user")
    if "--after-update" in sys.argv:          # даём старой версии закрыться
        for _ in range(40):
            probe = QLocalSocket()
            probe.connectToServer(server_name)
            if not probe.waitForConnected(200):
                break
            probe.abort()
            QThread.msleep(250)
    sock = QLocalSocket()
    sock.connectToServer(server_name)
    if sock.waitForConnected(300):        # уже запущено — просто показать окно
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(server_name)
    server = QLocalServer()
    server.listen(server_name)

    ctl = Controller(hidden="--hidden" in sys.argv)
    if "--after-update" in sys.argv:
        ctl.tray.showMessage(APP_NAME, f"Обновлено до версии {VERSION}", QSystemTrayIcon.Information, 5000)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), ctl.show_window()))
    app.setWindowIcon(ctl.icon_ok)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
