# ============================================================
#  PORT BOT CHILIAD v2.0
#  Полностью переписанный бот для тестовой среды GTA5RP
#  Автор: переработан Claude для собственного сервера
#  Не содержит: инжектора, читера памяти, обхода античита
# ============================================================

import json, os, sys, threading, time, queue, csv, traceback
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, colorchooser

try:
    import cv2
    import numpy as np
    import mss
    import pyautogui
    pyautogui.FAILSAFE = True      # двигай мышь в угол — аварийная остановка
    pyautogui.PAUSE   = 0.0
    IMPORT_ERROR = None
except Exception as e:
    cv2 = np = mss = pyautogui = None
    IMPORT_ERROR = str(e)

try:
    import keyboard as kb_lib
    HAS_KEYBOARD = True
except Exception:
    HAS_KEYBOARD = False

# ── пути ───────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)      # рядом с PORT_BOT_CHILIAD.exe
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "session_log.csv")

# ── дефолтный конфиг ───────────────────────────────────────
DEFAULTS = {
    # Захват экрана
    "screen": {"left": 0, "top": 0, "width": 1920, "height": 1080},

    # HSV диапазон красного маркера (красный — два диапазона в HSV)
    "marker_hsv_low":  [0,   120,  80],
    "marker_hsv_high": [10,  255, 255],
    "marker_hsv_low2": [170, 120,  80],
    "marker_hsv_high2":[180, 255, 255],

    # Площадь контура
    "marker_min_area": 150,    # пиксели²: ниже — шум
    "close_area":      3000,   # площадь, при которой «подошли достаточно»

    # Движение
    "horizontal_deadzone": 40,  # px от центра — не рулим
    "steer_gain":  0.003,       # усиление руля (пока зарезервировано)
    "approach_key": "w",
    "sprint_key":   "shift",    # держим shift для бега
    "left_key":     "a",
    "right_key":    "d",
    "interact_key": "e",

    # Мини-игра
    "minigame": {
        "enabled": True,
        # ROI: [x, y, width, height] — область на экране с ползунком
        "roi": [580, 340, 760, 280],
        # HSV зелёного ползунка
        "green_low":  [35, 60, 60],
        "green_high": [90, 255, 255],
        # HSV белой целевой зоны
        "white_low":  [0,   0, 200],
        "white_high": [180, 40, 255],
        # Сколько кадров подряд ползунок должен быть в зоне (антишум)
        "confirm_frames": 2,
        "action_key": "e",
        "timeout_seconds": 18
    },

    # Экономика
    "pay_per_box": 11.0,

    # Тайминги (секунды)
    "pickup_anim_wait":   2.5,  # ждём анимацию подбора
    "delivery_anim_wait": 1.8,  # пауза после сдачи перед поиском следующей
    "interaction_cooldown": 1.8,
    "max_run_minutes": 120,

    # Горячие клавиши (работают через keyboard-lib, только если установлена)
    "hotkey_start": "f6",
    "hotkey_stop":  "f7",

    # Логирование
    "csv_log": True,

    # Отладочное окно предпросмотра (только при запуске .py, не EXE)
    "debug_preview": False
}


# ╔══════════════════════════════════════════════════════════╗
# ║              ЗАГРУЗКА / СОХРАНЕНИЕ КОНФИГА              ║
# ╚══════════════════════════════════════════════════════════╝
def _deep_merge(dst, src):
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            _deep_merge(dst[k], v)

def load_config():
    if not os.path.exists(CFG_PATH):
        save_config(DEFAULTS)
        return json.loads(json.dumps(DEFAULTS))
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    _deep_merge(data, DEFAULTS)
    return data

def save_config(cfg):
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ╔══════════════════════════════════════════════════════════╗
# ║                    CSV ЛОГГЕР                           ║
# ╚══════════════════════════════════════════════════════════╝
class CsvLogger:
    HEADER = ["timestamp", "event", "boxes", "earned", "errors", "detail"]

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._lock = threading.Lock()
        if enabled and not os.path.exists(LOG_PATH):
            with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.HEADER)

    def write(self, event: str, boxes: int, earned: float, errors: int, detail: str = ""):
        if not self.enabled:
            return
        row = [datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               event, boxes, f"{earned:.2f}", errors, detail]
        with self._lock:
            with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)


# ╔══════════════════════════════════════════════════════════╗
# ║                      ЯДРО БОТА                          ║
# ╚══════════════════════════════════════════════════════════╝
class PortBot:
    """
    Конечный автомат (FSM) для цикла работы на порту:

    IDLE → FIND_PICKUP → APPROACH_PICKUP → WAIT_PICKUP_ANIM
         → FIND_DELIVERY → APPROACH_DELIVERY → WAIT_MINIGAME_START
         → MINIGAME → WAIT_DELIVERY_ANIM → FIND_PICKUP → …

    При ошибке: CYCLE_FAILED → FIND_PICKUP (без остановки)
    """

    STATES = {
        "IDLE":                "⏸  Ожидание запуска",
        "FIND_PICKUP":         "🔍 Поиск метки подбора",
        "APPROACH_PICKUP":     "🏃 Подход к коробке",
        "WAIT_PICKUP_ANIM":    "📦 Анимация подбора",
        "FIND_DELIVERY":       "🔍 Поиск метки доставки",
        "APPROACH_DELIVERY":   "🏃 Подход к точке доставки",
        "WAIT_MINIGAME_START": "⏳ Ожидание мини-игры",
        "MINIGAME":            "🎯 Мини-игра",
        "WAIT_DELIVERY_ANIM":  "✅ Завершение цикла",
        "CYCLE_FAILED":        "⚠️  Ошибка цикла — повтор",
    }

    def __init__(self, emit):
        self.emit       = emit       # callback: tuple → GUI
        self.stop_ev    = threading.Event()
        self._thread    = None
        self.running    = False

        # Статистика сессии
        self.boxes      = 0
        self.errors     = 0
        self.started    = None

        self.cfg        = load_config()
        self.logger     = CsvLogger(self.cfg.get("csv_log", True))
        self._pressed   = set()
        self._state     = "IDLE"

    # ── утилиты ──────────────────────────────────────────
    def _say(self, msg: str):
        self.emit(("log", f"[{datetime.now():%H:%M:%S}] {msg}"))

    def _set_state(self, s: str):
        self._state = s
        self.emit(("state", self.STATES.get(s, s)))

    def _emit_stats(self):
        earned = self.boxes * float(self.cfg["pay_per_box"])
        self.emit(("stats", self.boxes, earned, self.errors, self.started))

    # ── клавиши ──────────────────────────────────────────
    def _press(self, key: str):
        if pyautogui and key not in self._pressed:
            pyautogui.keyDown(key)
            self._pressed.add(key)

    def _release(self, *keys):
        if not pyautogui:
            return
        targets = keys if keys else list(self._pressed)
        for k in targets:
            if k in self._pressed:
                try:
                    pyautogui.keyUp(k)
                except Exception:
                    pass
                self._pressed.discard(k)

    def _release_all(self):
        self._release(*list(self._pressed))

    def _tap(self, key: str, hold: float = 0.08):
        self._release_all()
        pyautogui.keyDown(key)
        time.sleep(hold)
        pyautogui.keyUp(key)
        time.sleep(0.05)

    # ── захват экрана ─────────────────────────────────────
    def _screenshot(self) -> "np.ndarray":
        s = self.cfg["screen"]
        with mss.mss() as cap:
            shot = cap.grab({
                "left": int(s["left"]), "top": int(s["top"]),
                "width": int(s["width"]), "height": int(s["height"])
            })
        return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)

    # ── поиск красного маркера ────────────────────────────
    def _find_marker(self, frame: "np.ndarray"):
        """
        Возвращает (cx, cy, area) крупнейшего красного контура,
        или None если не найдено.
        """
        c   = self.cfg
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        m1  = cv2.inRange(hsv, np.array(c["marker_hsv_low"]),  np.array(c["marker_hsv_high"]))
        m2  = cv2.inRange(hsv, np.array(c["marker_hsv_low2"]), np.array(c["marker_hsv_high2"]))
        mask = cv2.bitwise_or(m1, m2)
        # Убираем мелкий шум
        kernel = np.ones((5, 5), np.uint8)
        mask   = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  kernel)
        mask   = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        valid = [x for x in contours if cv2.contourArea(x) >= float(c["marker_min_area"])]
        if not valid:
            return None
        cnt  = max(valid, key=cv2.contourArea)
        area = cv2.contourArea(cnt)
        M    = cv2.moments(cnt)
        if M["m00"] == 0:
            return None
        cx = int(M["m10"] / M["m00"])
        cy = int(M["m01"] / M["m00"])
        return cx, cy, area

    # ── движение к маркеру ────────────────────────────────
    def _move_toward(self, marker) -> bool:
        """
        Рулит к маркеру, держит W+Shift (бег).
        Возвращает True, если маркер «рядом» (area >= close_area).
        """
        cx, cy, area = marker
        c    = self.cfg
        w    = int(c["screen"]["width"])
        dx   = cx - w // 2
        dead = int(c["horizontal_deadzone"])

        self._release_all()

        # Всегда бежим вперёд
        self._press(c["sprint_key"])
        self._press(c["approach_key"])

        # Корректировка влево / вправо
        if dx < -dead:
            self._press(c["left_key"])
        elif dx > dead:
            self._press(c["right_key"])

        return area >= float(c["close_area"])

    # ── мини-игра ─────────────────────────────────────────
    def _play_minigame(self) -> bool:
        """
        Ждёт появления зелёного ползунка в белой зоне и нажимает клавишу.
        Требует confirm_frames кадров подряд — защита от шума.
        Возвращает True при успехе.
        """
        mg = self.cfg["minigame"]
        if not mg["enabled"]:
            self._tap(self.cfg["interact_key"])
            return True

        x0, y0, ww, hh = [int(v) for v in mg["roi"]]
        g_low  = np.array(mg["green_low"])
        g_high = np.array(mg["green_high"])
        w_low  = np.array(mg["white_low"])
        w_high = np.array(mg["white_high"])
        need   = int(mg.get("confirm_frames", 2))
        key    = mg["action_key"]
        t_out  = float(mg["timeout_seconds"])

        hit_streak = 0
        t_start    = time.time()

        while not self.stop_ev.is_set():
            if time.time() - t_start > t_out:
                self._say("⏱ Мини-игра: тайм-аут")
                return False

            frame = self._screenshot()
            roi   = frame[y0:y0 + hh, x0:x0 + ww]
            if roi.size == 0:
                self._say("⚠️ ROI мини-игры пустой — проверь config.json → minigame.roi")
                return False

            hsv   = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            green = cv2.inRange(hsv, g_low,  g_high)
            white = cv2.inRange(hsv, w_low,  w_high)

            gx = np.where(green > 0)[1]
            wx = np.where(white > 0)[1]

            if len(gx) > 5 and len(wx) > 10:
                slider_x  = float(np.median(gx))
                target_lo = float(np.percentile(wx, 5))
                target_hi = float(np.percentile(wx, 95))

                if target_lo <= slider_x <= target_hi:
                    hit_streak += 1
                    if hit_streak >= need:
                        self._release_all()
                        self._tap(key, hold=0.06)
                        time.sleep(0.4)
                        return True
                else:
                    hit_streak = 0
            else:
                hit_streak = 0

            time.sleep(0.018)   # ~55 fps

        return False

    # ── ожидание с возможностью прерывания ───────────────
    def _wait(self, seconds: float, step: float = 0.05):
        end = time.time() + seconds
        while not self.stop_ev.is_set() and time.time() < end:
            time.sleep(step)

    # ── главный цикл ─────────────────────────────────────
    def _run_loop(self):
        self.running = True
        self.started = time.time()
        self.boxes   = 0
        self.errors  = 0
        self._say("🟢 Агент запущен. Убедись, что рабочая смена начата у NPC Прораба.")
        self.logger.write("SESSION_START", 0, 0.0, 0)

        self._set_state("FIND_PICKUP")
        last_interact = 0.0
        fail_streak   = 0   # ошибки подряд → пауза
        c             = self.cfg

        try:
            while not self.stop_ev.is_set():
                # ── лимит времени ──
                elapsed_min = (time.time() - self.started) / 60
                if elapsed_min >= float(c["max_run_minutes"]):
                    self._say(f"⏱ Достигнут лимит {c['max_run_minutes']} мин. Остановка.")
                    break

                frame  = self._screenshot()
                marker = self._find_marker(frame)

                # ════ FIND_PICKUP ════
                if self._state == "FIND_PICKUP":
                    if marker is None:
                        self._release_all()
                        time.sleep(0.1)
                        continue
                    self._set_state("APPROACH_PICKUP")

                # ════ APPROACH_PICKUP ════
                elif self._state == "APPROACH_PICKUP":
                    if marker is None:
                        # Метка пропала — подбираем последнее положение, ждём
                        self._release_all()
                        time.sleep(0.15)
                        continue
                    close = self._move_toward(marker)
                    cool  = time.time() - last_interact > float(c["interaction_cooldown"])
                    if close and cool:
                        self._release_all()
                        self._tap(c["interact_key"])
                        last_interact = time.time()
                        self._say("📦 Коробка подобрана. Ожидание анимации…")
                        self._set_state("WAIT_PICKUP_ANIM")

                # ════ WAIT_PICKUP_ANIM ════
                elif self._state == "WAIT_PICKUP_ANIM":
                    self._release_all()
                    self._wait(float(c["pickup_anim_wait"]))
                    if not self.stop_ev.is_set():
                        self._set_state("FIND_DELIVERY")
                        self._say("🔍 Ищем точку доставки…")

                # ════ FIND_DELIVERY ════
                elif self._state == "FIND_DELIVERY":
                    if marker is None:
                        self._release_all()
                        time.sleep(0.1)
                        continue
                    self._set_state("APPROACH_DELIVERY")

                # ════ APPROACH_DELIVERY ════
                elif self._state == "APPROACH_DELIVERY":
                    if marker is None:
                        self._release_all()
                        time.sleep(0.15)
                        continue
                    close = self._move_toward(marker)
                    cool  = time.time() - last_interact > float(c["interaction_cooldown"])
                    if close and cool:
                        self._release_all()
                        self._tap(c["interact_key"])
                        last_interact = time.time()
                        self._say("📍 Точка доставки. Ожидание мини-игры…")
                        self._set_state("WAIT_MINIGAME_START")

                # ════ WAIT_MINIGAME_START ════
                # Даём серверу время показать UI мини-игры
                elif self._state == "WAIT_MINIGAME_START":
                    self._release_all()
                    self._wait(0.7)
                    if not self.stop_ev.is_set():
                        self._set_state("MINIGAME")

                # ════ MINIGAME ════
                elif self._state == "MINIGAME":
                    ok = self._play_minigame()
                    if ok:
                        self.boxes += 1
                        fail_streak = 0
                        earned = self.boxes * float(c["pay_per_box"])
                        self._emit_stats()
                        self.logger.write("BOX_OK", self.boxes, earned, self.errors)
                        self._say(f"✅ Цикл #{self.boxes} завершён. +${c['pay_per_box']:.0f}  |  Итого: ${earned:.2f}")
                        self._set_state("WAIT_DELIVERY_ANIM")
                    else:
                        self.errors += 1
                        fail_streak += 1
                        self._emit_stats()
                        self.logger.write("MINIGAME_FAIL", self.boxes,
                                          self.boxes * float(c["pay_per_box"]), self.errors)
                        self._say(f"⚠️ Мини-игра не пройдена (ошибок: {self.errors}). Повтор цикла…")
                        self._set_state("CYCLE_FAILED")

                # ════ WAIT_DELIVERY_ANIM ════
                elif self._state == "WAIT_DELIVERY_ANIM":
                    self._release_all()
                    self._wait(float(c["delivery_anim_wait"]))
                    if not self.stop_ev.is_set():
                        self._set_state("FIND_PICKUP")

                # ════ CYCLE_FAILED ════
                elif self._state == "CYCLE_FAILED":
                    self._release_all()
                    if fail_streak >= 5:
                        self._say("🛑 5 ошибок подряд — автоматическая остановка для безопасности.")
                        self.logger.write("AUTO_STOP_FAIL", self.boxes,
                                          self.boxes * float(c["pay_per_box"]), self.errors)
                        break
                    self._wait(2.0)
                    if not self.stop_ev.is_set():
                        self._set_state("FIND_PICKUP")

                time.sleep(0.025)

        except Exception as e:
            self.errors += 1
            tb = traceback.format_exc()
            self._say(f"💥 Аварийная ошибка: {e}\n{tb}")
            self.logger.write("CRASH", self.boxes,
                              self.boxes * float(c["pay_per_box"]), self.errors, str(e))
        finally:
            self._release_all()
            self.running = False
            earned = self.boxes * float(c["pay_per_box"])
            self.logger.write("SESSION_END", self.boxes, earned, self.errors,
                              f"elapsed={int(time.time()-self.started)}s")
            self._emit_stats()
            self._set_state("IDLE")
            self.emit(("stopped",))
            self._say(f"🔴 Агент остановлен. Итог: {self.boxes} коробок, ${earned:.2f}, ошибок: {self.errors}")

    # ── публичные методы ─────────────────────────────────
    def start(self):
        if self.running:
            return
        self.cfg = load_config()
        self.logger = CsvLogger(self.cfg.get("csv_log", True))
        self.stop_ev.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_ev.set()
        self._release_all()


# ╔══════════════════════════════════════════════════════════╗
# ║           МАСТЕР КАЛИБРОВКИ (встроенный)                ║
# ╚══════════════════════════════════════════════════════════╝
class CalibrationWizard(tk.Toplevel):
    """
    Пошаговый мастер для ввода основных параметров без ручного
    редактирования JSON. Сохраняет в config.json.
    """
    def __init__(self, master, cfg, on_save):
        super().__init__(master)
        self.title("Мастер калибровки")
        self.resizable(False, False)
        self.grab_set()
        self.cfg    = cfg
        self.on_save = on_save
        self._build()

    def _row(self, parent, label, default):
        f = ttk.Frame(parent)
        f.pack(fill="x", pady=3)
        ttk.Label(f, text=label, width=28, anchor="w").pack(side="left")
        var = tk.StringVar(value=str(default))
        ttk.Entry(f, textvariable=var, width=14).pack(side="left")
        return var

    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        # Вкладка 1 — Экран
        p1 = ttk.Frame(nb, padding=10)
        nb.add(p1, text="📐 Экран")
        s = self.cfg["screen"]
        ttk.Label(p1, text="Параметры захвата экрана\n(для полного монитора оставь как есть)",
                  foreground="#555").pack(anchor="w", pady=(0,8))
        self._v_sleft   = self._row(p1, "Отступ слева (left)",   s["left"])
        self._v_stop    = self._row(p1, "Отступ сверху (top)",   s["top"])
        self._v_swidth  = self._row(p1, "Ширина (width)",         s["width"])
        self._v_sheight = self._row(p1, "Высота (height)",        s["height"])

        # Вкладка 2 — Маркер
        p2 = ttk.Frame(nb, padding=10)
        nb.add(p2, text="🔴 Маркер")
        ttk.Label(p2, text="HSV диапазон красного маркера\nЗначения: [H, S, V]",
                  foreground="#555").pack(anchor="w", pady=(0,8))
        self._v_mlow  = self._row(p2, "Нижний порог (low)",  str(self.cfg["marker_hsv_low"]))
        self._v_mhigh = self._row(p2, "Верхний порог (high)",str(self.cfg["marker_hsv_high"]))
        self._v_mlow2 = self._row(p2, "Нижний порог 2",      str(self.cfg["marker_hsv_low2"]))
        self._v_mhi2  = self._row(p2, "Верхний порог 2",     str(self.cfg["marker_hsv_high2"]))
        self._v_minar = self._row(p2, "Мин. площадь контура",self.cfg["marker_min_area"])
        self._v_close = self._row(p2, "Площадь «рядом»",     self.cfg["close_area"])

        # Вкладка 3 — Мини-игра
        p3 = ttk.Frame(nb, padding=10)
        nb.add(p3, text="🎯 Мини-игра")
        ttk.Label(p3, text="ROI = [x, y, width, height] — область ползунка на экране",
                  foreground="#555").pack(anchor="w", pady=(0,8))
        mg = self.cfg["minigame"]
        self._v_roi    = self._row(p3, "ROI [x, y, w, h]",   str(mg["roi"]))
        self._v_glow   = self._row(p3, "Зелёный HSV low",     str(mg["green_low"]))
        self._v_ghigh  = self._row(p3, "Зелёный HSV high",    str(mg["green_high"]))
        self._v_wlow   = self._row(p3, "Белый HSV low",       str(mg["white_low"]))
        self._v_whigh  = self._row(p3, "Белый HSV high",      str(mg["white_high"]))
        self._v_cfr    = self._row(p3, "Confirm frames (1-5)",mg.get("confirm_frames",2))
        self._v_mgkey  = self._row(p3, "Клавиша действия",    mg["action_key"])
        self._v_tmt    = self._row(p3, "Тайм-аут (сек)",      mg["timeout_seconds"])

        # Вкладка 4 — Тайминги
        p4 = ttk.Frame(nb, padding=10)
        nb.add(p4, text="⏱ Тайминги")
        self._v_pickup  = self._row(p4, "Пауза анимации подбора (с)",   self.cfg["pickup_anim_wait"])
        self._v_deliver = self._row(p4, "Пауза завершения цикла (с)",   self.cfg["delivery_anim_wait"])
        self._v_cooldown= self._row(p4, "Кулдаун взаимодействия (с)",   self.cfg["interaction_cooldown"])
        self._v_maxmin  = self._row(p4, "Макс. время работы (мин)",      self.cfg["max_run_minutes"])
        self._v_pay     = self._row(p4, "Оплата за коробку ($)",         self.cfg["pay_per_box"])

        # Вкладка 5 — Горячие клавиши
        p5 = ttk.Frame(nb, padding=10)
        nb.add(p5, text="⌨ Горячие клавиши")
        ttk.Label(p5, text="Работают только при установленной библиотеке 'keyboard'\n"
                  "Добавь 'keyboard' в requirements.txt для активации",
                  foreground="#555").pack(anchor="w", pady=(0,8))
        self._v_hkstart = self._row(p5, "Старт (hotkey_start)", self.cfg.get("hotkey_start","f6"))
        self._v_hkstop  = self._row(p5, "Стоп  (hotkey_stop)",  self.cfg.get("hotkey_stop", "f7"))
        ttk.Label(p5, text="Состояние модуля keyboard: " +
                  ("✅ установлен" if HAS_KEYBOARD else "❌ не установлен"),
                  foreground="green" if HAS_KEYBOARD else "red").pack(anchor="w", pady=(12,0))

        # Кнопки
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=(0,10))
        ttk.Button(btn_frame, text="💾 Сохранить", command=self._save).pack(side="right")
        ttk.Button(btn_frame, text="Отмена",       command=self.destroy).pack(side="right", padx=6)

    def _parse_list(self, s):
        s = s.strip().strip("[]")
        return [float(x) for x in s.split(",")]

    def _save(self):
        try:
            c = self.cfg
            c["screen"] = {
                "left":   int(self._v_sleft.get()),
                "top":    int(self._v_stop.get()),
                "width":  int(self._v_swidth.get()),
                "height": int(self._v_sheight.get()),
            }
            c["marker_hsv_low"]  = self._parse_list(self._v_mlow.get())
            c["marker_hsv_high"] = self._parse_list(self._v_mhigh.get())
            c["marker_hsv_low2"] = self._parse_list(self._v_mlow2.get())
            c["marker_hsv_high2"]= self._parse_list(self._v_mhi2.get())
            c["marker_min_area"] = float(self._v_minar.get())
            c["close_area"]      = float(self._v_close.get())

            mg = c["minigame"]
            mg["roi"]            = self._parse_list(self._v_roi.get())
            mg["green_low"]      = self._parse_list(self._v_glow.get())
            mg["green_high"]     = self._parse_list(self._v_ghigh.get())
            mg["white_low"]      = self._parse_list(self._v_wlow.get())
            mg["white_high"]     = self._parse_list(self._v_whigh.get())
            mg["confirm_frames"] = int(self._v_cfr.get())
            mg["action_key"]     = self._v_mgkey.get().strip()
            mg["timeout_seconds"]= float(self._v_tmt.get())

            c["pickup_anim_wait"]    = float(self._v_pickup.get())
            c["delivery_anim_wait"]  = float(self._v_deliver.get())
            c["interaction_cooldown"]= float(self._v_cooldown.get())
            c["max_run_minutes"]     = float(self._v_maxmin.get())
            c["pay_per_box"]         = float(self._v_pay.get())
            c["hotkey_start"]        = self._v_hkstart.get().strip()
            c["hotkey_stop"]         = self._v_hkstop.get().strip()

            save_config(c)
            self.on_save(c)
            messagebox.showinfo("Калибровка", "Настройки сохранены в config.json ✅", parent=self)
            self.destroy()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{e}", parent=self)


# ╔══════════════════════════════════════════════════════════╗
# ║                   ГЛАВНЫЙ GUI                           ║
# ╚══════════════════════════════════════════════════════════╝
class App:
    def __init__(self, root: tk.Tk):
        self.root   = root
        root.title("PORT BOT CHILIAD v2.0")
        root.geometry("800x620")
        root.minsize(700, 520)
        self._events = queue.Queue()
        self.bot     = PortBot(self._events.put)
        self._build()
        self._register_hotkeys()
        self._poll()

    # ── построение интерфейса ─────────────────────────────
    def _build(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        # ─ Заголовок ─
        hdr = tk.Frame(root, bg="#1a1a2e")
        hdr.grid(row=0, column=0, sticky="ew")
        tk.Label(hdr, text="PORT BOT  CHILIAD  v2.0",
                 font=("Segoe UI", 16, "bold"), bg="#1a1a2e", fg="#e0e0ff",
                 pady=10).pack(side="left", padx=16)
        tk.Label(hdr, text="Тестовая среда · Только собственный сервер",
                 font=("Segoe UI", 9), bg="#1a1a2e", fg="#8888aa",
                 pady=10).pack(side="left")

        # ─ Тело ─
        body = ttk.Frame(root, padding=10)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)

        # Карточки статистики
        stats_row = ttk.Frame(body)
        stats_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self._vars = {}
        cards = [
            ("state",  "Состояние",    "⏸  Остановлен"),
            ("boxes",  "Коробок",      "0"),
            ("earned", "Начислено*",   "$0.00"),
            ("errors", "Ошибок",       "0"),
            ("rate",   "Темп*",        "$0/ч"),
            ("time",   "Время сессии", "00:00"),
        ]
        for i, (key, label, default) in enumerate(cards):
            frame = ttk.LabelFrame(stats_row, text=label, padding=(8, 4))
            frame.grid(row=0, column=i, padx=4, sticky="ew")
            stats_row.columnconfigure(i, weight=1)
            var = tk.StringVar(value=default)
            self._vars[key] = var
            font = ("Segoe UI", 13, "bold") if key != "state" else ("Segoe UI", 9, "bold")
            tk.Label(frame, textvariable=var, font=font).pack()

        # Панель управления
        ctrl = ttk.Frame(body)
        ctrl.grid(row=1, column=0, sticky="ew", pady=(0, 6))

        self._btn_start = ttk.Button(ctrl, text="▶  СТАРТ  (F6)", command=self.start, width=18)
        self._btn_start.pack(side="left")

        self._btn_stop = ttk.Button(ctrl, text="⏹  СТОП  (F7)", command=self.stop,
                                     width=16, state="disabled")
        self._btn_stop.pack(side="left", padx=6)

        ttk.Separator(ctrl, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Button(ctrl, text="⚙  Калибровка",  command=self.open_calibration).pack(side="left")
        ttk.Button(ctrl, text="📄 Открыть лог", command=self.open_log).pack(side="left", padx=6)
        ttk.Button(ctrl, text="🗂 config.json",  command=self.open_config).pack(side="left")

        # Лог
        log_frame = ttk.LabelFrame(body, text="Журнал событий", padding=4)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self._log = tk.Text(log_frame, wrap="word", state="disabled",
                            font=("Consolas", 9), bg="#0d0d1a", fg="#c8c8e8",
                            insertbackground="white")
        sb = ttk.Scrollbar(log_frame, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        self._log.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        # Нижняя строка
        foot = ttk.Frame(body)
        foot.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(foot,
                  text="* Сумма и темп расчётные. Укажи фактическую оплату в Калибровке.",
                  foreground="#888").pack(side="left")
        ttk.Label(foot,
                  text="Аварийная остановка: мышь в угол экрана",
                  foreground="#c06050").pack(side="right")

        # Таймер обновления времени
        self._update_time()

    # ── горячие клавиши ───────────────────────────────────
    def _register_hotkeys(self):
        if not HAS_KEYBOARD:
            return
        c = self.bot.cfg
        try:
            kb_lib.add_hotkey(c.get("hotkey_start", "f6"),
                              lambda: self.root.after(0, self.start), suppress=False)
            kb_lib.add_hotkey(c.get("hotkey_stop",  "f7"),
                              lambda: self.root.after(0, self.stop),  suppress=False)
        except Exception as e:
            self._append(f"[!] Горячие клавиши не зарегистрированы: {e}")

    # ── действия ─────────────────────────────────────────
    def start(self):
        if IMPORT_ERROR:
            messagebox.showerror(
                "Библиотеки не установлены",
                f"{IMPORT_ERROR}\n\nЗапустите install.bat и перезапустите программу.")
            return
        if self.bot.running:
            return
        if not messagebox.askyesno(
                "Подтверждение запуска",
                "Запустить агент?\n\n"
                "Убедись:\n"
                "• Игра запущена в ОКОННОМ режиме\n"
                "• Смена у NPC Прораба уже начата\n"
                "• Первый красный маркер виден\n"
                "• config.json откалиброван\n\n"
                "Аварийная остановка: мышь в угол экрана"):
            return
        self._vars["state"].set("🟡 Запуск…")
        self._btn_start.configure(state="disabled")
        self._btn_stop.configure(state="normal")
        self.bot.start()

    def stop(self):
        self.bot.stop()
        self._vars["state"].set("🔴 Остановка…")
        self._btn_start.configure(state="normal")
        self._btn_stop.configure(state="disabled")

    def open_calibration(self):
        CalibrationWizard(self.root, self.bot.cfg, self._on_config_saved)

    def _on_config_saved(self, new_cfg):
        self.bot.cfg = new_cfg
        self._append("[i] Конфиг обновлён из мастера калибровки.")

    def open_config(self):
        try:
            os.startfile(CFG_PATH)
        except Exception:
            messagebox.showinfo("config.json", CFG_PATH)

    def open_log(self):
        if not os.path.exists(LOG_PATH):
            messagebox.showinfo("Лог", "Лог-файл пока не создан (нет завершённых циклов).")
            return
        try:
            os.startfile(LOG_PATH)
        except Exception:
            messagebox.showinfo("Лог", LOG_PATH)

    # ── GUI ───────────────────────────────────────────────
    def _append(self, msg: str):
        self._log.configure(state="normal")
        self._log.insert("end", msg + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _update_time(self):
        if self.bot.running and self.bot.started:
            elapsed = int(time.time() - self.bot.started)
            m, s    = divmod(elapsed, 60)
            self._vars["time"].set(f"{m:02d}:{s:02d}")
        self.root.after(1000, self._update_time)

    def _poll(self):
        try:
            while True:
                item = self._events.get_nowait()
                tag  = item[0]
                if tag == "log":
                    self._append(item[1])
                elif tag == "state":
                    self._vars["state"].set(item[1])
                elif tag == "stopped":
                    self._btn_start.configure(state="normal")
                    self._btn_stop.configure(state="disabled")
                elif tag == "stats":
                    _, boxes, earned, errors, started = item
                    self._vars["boxes"].set(str(boxes))
                    self._vars["earned"].set(f"${earned:.2f}")
                    self._vars["errors"].set(str(errors))
                    elapsed = max(1, time.time() - (started or time.time()))
                    rate    = earned * 3600 / elapsed
                    self._vars["rate"].set(f"${rate:.0f}/ч")
        except queue.Empty:
            pass
        self.root.after(120, self._poll)


# ╔══════════════════════════════════════════════════════════╗
# ║                      ТОЧКА ВХОДА                        ║
# ╚══════════════════════════════════════════════════════════╝
def main():
    if IMPORT_ERROR:
        # Даже без библиотек — показываем GUI с инструкцией
        root = tk.Tk()
        root.title("PORT BOT CHILIAD v2.0 — ошибка")
        root.geometry("500x200")
        tk.Label(root, text="Не установлены зависимости:", font=("Segoe UI", 11, "bold"),
                 fg="red").pack(pady=(20,4))
        tk.Label(root, text=IMPORT_ERROR, font=("Consolas", 9)).pack()
        tk.Label(root, text="Запустите install.bat и перезапустите программу.",
                 font=("Segoe UI", 10)).pack(pady=8)
        ttk.Button(root, text="Закрыть", command=root.destroy).pack()
        root.mainloop()
        return

    root = tk.Tk()
    app  = App(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.stop(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
