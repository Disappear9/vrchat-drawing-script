# ==============================================================================
# 依赖库安装:
# pip install opencv-python numpy Pillow scikit-image keyboard pywin32
# ==============================================================================
from math import hypot, ceil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, TclError
import cv2
import numpy as np
from PIL import Image
import threading
import time
import win32api
import win32gui
import win32con
import keyboard
import os
import copy
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# --- 数据结构 (无变化) ---
@dataclass
class DrawingPoint:
    x: float
    y: float

@dataclass
class DrawingStroke:
    points: List[DrawingPoint] = field(default_factory=list)

# --- 核心算法：图像矢量化 (无变化) ---
class ImageVectorizer:
    def __init__(self):
        self.binary_threshold = 128
        self.simplify_epsilon = 1.5
        self.min_stroke_length = 5
        self.image_height = 0
        self.image_width = 0

    def _trace_path(self, y_start: int, x_start: int, skeleton: np.ndarray, visited_mask: np.ndarray) -> List[DrawingPoint]:
        path_points = []
        rows, cols = skeleton.shape
        current_y, current_x = y_start, x_start
        while True:
            if visited_mask[current_y, current_x]: break
            visited_mask[current_y, current_x] = True
            path_points.append(DrawingPoint(x=float(current_x), y=float(current_y)))
            found_next = False
            for dy in [-1, 0, 1]:
                for dx in [-1, 0, 1]:
                    if dy == 0 and dx == 0: continue
                    next_y, next_x = current_y + dy, current_x + dx
                    if 0 <= next_y < rows and 0 <= next_x < cols:
                        if skeleton[next_y, next_x] > 0 and not visited_mask[next_y, next_x]:
                            current_y, current_x = next_y, next_x
                            found_next = True
                            break
                if found_next: break
            if not found_next: break
        return path_points

    def process_image(self, image_path: str) -> Optional[List[DrawingStroke]]:
        try:
            from skimage.morphology import skeletonize
            image = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
            if image is None:
                print(f"错误: 无法读取图片路径 '{image_path}'")
                return None
            
            self.image_height, self.image_width = image.shape[:2]

            _, binary_image = cv2.threshold(image, self.binary_threshold, 255, cv2.THRESH_BINARY_INV)
            binary_image[binary_image == 255] = 1
            skeleton = skeletonize(binary_image)
            skeleton = skeleton.astype(np.uint8) * 255
            
            visited_mask = np.zeros_like(skeleton, dtype=bool)
            raw_strokes = []
            rows, cols = skeleton.shape
            for y in range(rows):
                for x in range(cols):
                    if skeleton[y, x] > 0 and not visited_mask[y, x]:
                        path_points = self._trace_path(y, x, skeleton, visited_mask)
                        if len(path_points) >= self.min_stroke_length:
                            raw_strokes.append(DrawingStroke(points=path_points))
            
            if not raw_strokes: return []

            simplified_strokes = []
            for stroke in raw_strokes:
                contour = np.array([[p.x, p.y] for p in stroke.points]).reshape((-1, 1, 2)).astype(np.int32)
                simplified_contour = cv2.approxPolyDP(contour, self.simplify_epsilon, False)
                simplified_points = [DrawingPoint(float(p[0][0]), float(p[0][1])) for p in simplified_contour]
                if len(simplified_points) > 1:
                    simplified_strokes.append(DrawingStroke(points=simplified_points))
            
            if not simplified_strokes: return []

            sorted_strokes = []
            remaining_strokes = simplified_strokes.copy()
            remaining_strokes.sort(key=lambda s: hypot(s.points[0].x, s.points[0].y))
            current_stroke = remaining_strokes.pop(0)
            sorted_strokes.append(current_stroke)
            
            while remaining_strokes:
                last_point = current_stroke.points[-1]
                best_next_idx, min_dist, reverse_needed = -1, float('inf'), False

                for i, s in enumerate(remaining_strokes):
                    dist_to_start = hypot(s.points[0].x - last_point.x, s.points[0].y - last_point.y)
                    if dist_to_start < min_dist:
                        min_dist = dist_to_start
                        best_next_idx = i
                        reverse_needed = False

                    dist_to_end = hypot(s.points[-1].x - last_point.x, s.points[-1].y - last_point.y)
                    if dist_to_end < min_dist:
                        min_dist = dist_to_end
                        best_next_idx = i
                        reverse_needed = True

                next_stroke = remaining_strokes.pop(best_next_idx)
                if reverse_needed:
                    next_stroke.points.reverse()
                
                sorted_strokes.append(next_stroke)
                current_stroke = next_stroke
                
            return sorted_strokes
        except Exception as e:
            import traceback
            print(f"图像处理失败: {e}")
            traceback.print_exc()
            return None

# --- 核心重构：基于平面映射的 VRChat 绘图器 (无变化) ---
class VRChatDrawerPlanar:
    def __init__(self, vectorizer_ref):
        self.drawing_active = False
        self.drawing_thread: Optional[threading.Thread] = None
        self.current_strokes: List[DrawingStroke] = []
        self.vectorizer = vectorizer_ref

        # 平面映射参数
        self.sensitivity = 1.2
        self.point_delay = 0.031
        self.lift_pen_speed = 40.0

        # 平面状态变量
        self.current_x_px: float = 0.0
        self.current_y_px: float = 0.0
        self.error_x: float = 0.0
        self.error_y: float = 0.0

    def set_strokes(self, strokes: List[DrawingStroke]):
        self.current_strokes = strokes

    def focus_vrchat_window(self) -> bool:
        hwnd = win32gui.FindWindow(None, "VRChat")
        if hwnd:
            try:
                if win32gui.IsIconic(hwnd): win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                time.sleep(0.5)
                return win32gui.GetForegroundWindow() == hwnd
            except Exception as e:
                print(f"聚焦窗口失败: {e}")
                return False
        return False
    
    def start_drawing(self):
        if self.drawing_active: return
        if not self.current_strokes:
            messagebox.showinfo("提示", "没有可供绘画的笔画...")
            return
        if not hasattr(self.vectorizer, 'image_width') or self.vectorizer.image_width == 0:
            messagebox.showerror("错误", "未找到图像尺寸信息，请先成功处理一张图片。")
            return

#        if not self.focus_vrchat_window():
#            messagebox.showwarning("警告", "无法聚焦到VRChat窗口。")
#            return
        
        self.drawing_active = True
        self.drawing_thread = threading.Thread(target=self._draw_thread, daemon=True)
        self.drawing_thread.start()

    def stop_drawing(self):
        if self.drawing_active:
            print("正在停止绘画...")
            self.drawing_active = False
            if self.drawing_thread and self.drawing_thread.is_alive():
                self.drawing_thread.join(timeout=2.0)
            try:
                x,y = win32api.GetCursorPos()
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
                print("绘画已停止。")
            except Exception as e:
                print(f"停止时清理鼠标状态出错: {e}")

    def _move_to_target_pixel(self, target_x: float, target_y: float, is_pen_up: bool):
        if not self.drawing_active: return
        delta_px_x = target_x - self.current_x_px
        delta_px_y = target_y - self.current_y_px
        if abs(delta_px_x) < 1e-5 and abs(delta_px_y) < 1e-5: return
        ideal_dx_float = delta_px_x * self.sensitivity
        ideal_dy_float = delta_px_y * self.sensitivity
        if is_pen_up:
            if self.lift_pen_speed >= 100.0: num_steps = 1
            else:
                total_mouse_dist = hypot(ideal_dx_float, ideal_dy_float)
                max_pixels_per_step = 5 + (self.lift_pen_speed / 100.0) * 145 
                num_steps = max(1, int(ceil(total_mouse_dist / max_pixels_per_step)))
        else: num_steps = 1
        dx_per_step = ideal_dx_float / num_steps
        dy_per_step = ideal_dy_float / num_steps
        for _ in range(num_steps):
            if not self.drawing_active: break
            total_dx_to_move = dx_per_step + self.error_x
            total_dy_to_move = dy_per_step + self.error_y
            move_dx = int(round(total_dx_to_move))
            move_dy = int(round(total_dy_to_move))
            self.error_x = total_dx_to_move - move_dx
            self.error_y = total_dy_to_move - move_dy
            if move_dx != 0 or move_dy != 0: win32api.mouse_event(win32con.MOUSEEVENTF_MOVE, move_dx, move_dy, 0, 0)
            delay = 0.01 if is_pen_up else self.point_delay
            time.sleep(delay)
        self.current_x_px, self.current_y_px = target_x, target_y

    def _draw_thread(self):
        try:
            print("绘图线程启动... 3秒后开始，请将VRChat画笔对准【画布中心】！")
            time.sleep(3)
            if not self.drawing_active: return
            x,y = win32api.GetCursorPos()
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
            self.current_x_px = self.vectorizer.image_width / 2.0
            
            all_points = [p for s in self.current_strokes for p in s.points if s.points]
            if all_points:
                min_y = min(p.y for p in all_points)
                max_y = max(p.y for p in all_points)
                self.current_y_px = (min_y + max_y) / 2.0
            else: self.current_y_px = self.vectorizer.image_height / 2.0
            
            self.error_x, self.error_y = 0.0, 0.0

            total_strokes = len(self.current_strokes)
            for stroke_idx, stroke in enumerate(self.current_strokes):
                if not self.drawing_active or not stroke.points: break
                print(f"正在处理笔划 {stroke_idx + 1}/{total_strokes}...")
                x,y = win32api.GetCursorPos()
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
                time.sleep(0.05)
                target_point = stroke.points[0]
                print(f"  - 提笔移动至: ({target_point.x:.1f}, {target_point.y:.1f})")
                self._move_to_target_pixel(target_point.x, target_point.y, is_pen_up=True)
                if not self.drawing_active: break
                
                # =================================================================
                # 【重要修复】解决落笔时轻微移动的问题 v7.2.1
                # 增加一个更长的“沉降延时”，确保在执行 mouse_down 之前，
                # 鼠标光标在物理上已经完全停止移动。
                # 原延时为 0.05 秒，现增加至 0.1 秒。
                time.sleep(0.1) 
                # =================================================================

                if not self.drawing_active: break # 在延时后再次检查状态
                x,y = win32api.GetCursorPos()
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
                # 保留一个短暂延时，确保游戏引擎正确识别到“按下”状态再开始移动
                time.sleep(0.05)
                
                for point in stroke.points[1:]:
                    if not self.drawing_active: break
                    self._move_to_target_pixel(point.x, point.y, is_pen_up=False)
                if not self.drawing_active: break
                x,y = win32api.GetCursorPos()
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
                time.sleep(0.05)
                
            if self.drawing_active: print("所有笔划绘制完毕。")
            
        except Exception as e:
            import traceback
            print(f"绘图过程中出现严重错误: {e}")
            traceback.print_exc()
        finally:
            self.drawing_active = False
            try:
                x,y = win32api.GetCursorPos()
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
                print("绘图线程结束。")
                self.update_status("绘图线程结束。")
            except Exception as e:
                print(f"线程结束时释放鼠标按键出错: {e}")

# --- 用户界面 (UI) ---
class DrawingToolGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("VRChat 高级画图工具 v7.3 (win32api版)")
        self.root.geometry("850x900")
        self.vectorizer = ImageVectorizer()
        self.drawer = VRChatDrawerPlanar(self.vectorizer)
        self.current_image_path: Optional[str] = None
        self.original_strokes: List[DrawingStroke] = [] # 唯一的笔画数据源，用于预览
        self.setup_ui()
        self.setup_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _create_param_entry(self, parent, label_text, tk_var, from_, to, digits=1):
        frame = ttk.Frame(parent)
        ttk.Label(frame, text=label_text, width=15).pack(side='left', anchor='w')
        entry = ttk.Entry(frame, width=8, textvariable=tk_var)
        entry.pack(side='right', padx=(5, 0))
        scale = ttk.Scale(frame, from_=from_, to=to, variable=tk_var, orient='horizontal')
        scale.pack(side='right', fill='x', expand=True)
        format_str = f"{{:.{digits}f}}" if digits > 0 else "{:.0f}"
        def update_from_scale(value):
            try: tk_var.set(format_str.format(float(value)))
            except (ValueError, TclError): pass
        def finalize_entry(*args):
            try:
                val = float(tk_var.get())
                if val < from_: val = from_
                if val > to: val = to
                tk_var.set(format_str.format(val))
            except (ValueError, TclError):
                try: tk_var.set(format_str.format(scale.get()))
                except (ValueError, TclError): tk_var.set(format_str.format(from_))
        scale.config(command=update_from_scale)
        entry.bind("<FocusOut>", finalize_entry)
        entry.bind("<Return>", finalize_entry)
        frame.pack(fill='x', pady=2)
        return frame

    def setup_ui(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill='both', expand=True)
        
        control_panel = ttk.Frame(main_frame, padding="10")
        control_panel.pack(side='left', fill='y', anchor='n', expand=False, ipadx=5)
        
        file_frame = ttk.LabelFrame(control_panel, text="1. 选择图片", padding=10)
        file_frame.pack(fill='x', pady=5)
        ttk.Button(file_frame, text="打开图片文件", command=self.load_image).pack(fill='x')
        self.file_label = ttk.Label(file_frame, text="尚未选择文件", wraplength=200)
        self.file_label.pack(pady=5)
        
        proc_frame = ttk.LabelFrame(control_panel, text="2. 图像处理", padding=10)
        proc_frame.pack(fill='x', pady=5)
        self.threshold_var = tk.DoubleVar(value=self.vectorizer.binary_threshold)
        self._create_param_entry(proc_frame, "二值化阈值:", self.threshold_var, 1, 254, 0)
        self.epsilon_var = tk.DoubleVar(value=self.vectorizer.simplify_epsilon)
        self._create_param_entry(proc_frame, "路径简化度:", self.epsilon_var, 0.1, 10.0, 1)
        self.max_dist_var = tk.DoubleVar(value=10.0) 
        self._create_param_entry(proc_frame, "最大点距(插值):", self.max_dist_var, 1, 50, 1)
        ttk.Button(proc_frame, text="处理图片并生成笔画", command=self.process_image).pack(pady=10, fill='x')
        
        draw_frame = ttk.LabelFrame(control_panel, text="3. 绘画参数", padding=10)
        draw_frame.pack(fill='x', pady=5)
        self.sensitivity_var = tk.DoubleVar(value=self.drawer.sensitivity)
        self._create_param_entry(draw_frame, "绘画灵敏度:", self.sensitivity_var, 0.1, 3.0, 2)
        self.delay_var = tk.DoubleVar(value=self.drawer.point_delay * 1000)
        self._create_param_entry(draw_frame, "点间延迟 (ms):", self.delay_var, 1, 100, 0)
        self.lift_speed_var = tk.DoubleVar(value=self.drawer.lift_pen_speed)
        self._create_param_entry(draw_frame, "提笔速度 (%):", self.lift_speed_var, 1, 100, 0)
        self.stretch_var = tk.DoubleVar(value=1.4)
        # 注意：此滑块不再绑定实时更新预览的事件
        self._create_param_entry(draw_frame, "垂直拉伸:", self.stretch_var, 0.2, 3.0, 2)

        exec_frame = ttk.LabelFrame(control_panel, text="4. 执行控制 (全局热键)", padding=10)
        exec_frame.pack(fill='x', pady=5)
        ttk.Button(exec_frame, text="开始绘画 (F9)", command=self.start_drawing).pack(fill='x', pady=2)
        ttk.Button(exec_frame, text="强制停止 (F10)", command=self.stop_drawing).pack(fill='x', pady=2)
        
        self.status_label = ttk.Label(control_panel, text="状态: 就绪", relief='sunken', anchor='w', padding=5)
        self.status_label.pack(fill='x', pady=10, side='bottom')
        # --- 作者信息框 ---
        author_frame = ttk.LabelFrame(control_panel, text="关于", padding=10)
        author_frame.pack(fill='x', pady=5)

        ttk.Label(author_frame, text="改用win32api，理论上不会招致账户被封禁").pack(anchor='w')
        ttk.Label(author_frame, text="仅供技术交流").pack(anchor='w')
        ttk.Label(author_frame, text="https://space.bilibili.com/5145514").pack(anchor='w')
        ttk.Label(author_frame, text="Fork：Disappear9/vrchat-drawing-script").pack(anchor='w')
		
        right_panel = ttk.Frame(main_frame, padding="10")
        right_panel.pack(side='right', fill='both', expand=True)
        preview_frame = ttk.LabelFrame(right_panel, text="笔画预览 (2D - 原始比例)", padding=10)
        preview_frame.pack(fill='both', expand=True, pady=5)
        self.preview_canvas = tk.Canvas(preview_frame, bg='white')
        self.preview_canvas.pack(fill='both', expand=True)

    def setup_hotkeys(self):
        try:
            keyboard.add_hotkey('f9', self.start_drawing, suppress=True)
            keyboard.add_hotkey('f10', self.stop_drawing, suppress=True)
            print("快捷键 F9 (开始) 和 F10 (停止) 已设置。")
        except Exception as e:
            messagebox.showerror("快捷键错误", f"设置全局快捷键失败: {e}")

    def interpolate_strokes(self, strokes: List[DrawingStroke], max_distance: float) -> List[DrawingStroke]:
        if max_distance <= 1: return strokes
        new_strokes = []
        for stroke in strokes:
            if len(stroke.points) < 2:
                new_strokes.append(stroke)
                continue
            new_points = [stroke.points[0]]
            for i in range(len(stroke.points) - 1):
                p1, p2 = stroke.points[i], stroke.points[i+1]
                dist = hypot(p2.x - p1.x, p2.y - p1.y)
                if dist > max_distance:
                    num_segments = int(dist / max_distance) + 1
                    for j in range(1, num_segments):
                        ratio = j / num_segments
                        inter_x = p1.x + ratio * (p2.x - p1.x)
                        inter_y = p1.y + ratio * (p2.y - p1.y)
                        new_points.append(DrawingPoint(x=inter_x, y=inter_y))
                new_points.append(p2)
            unique_points = []
            if new_points:
                unique_points.append(new_points[0])
                for k in range(1, len(new_points)):
                    if new_points[k].x != unique_points[-1].x or new_points[k].y != unique_points[-1].y:
                        unique_points.append(new_points[k])
            new_strokes.append(DrawingStroke(points=unique_points))
        return new_strokes

    def _apply_vertical_stretch(self, strokes: List[DrawingStroke]) -> List[DrawingStroke]:
        """对笔画副本应用垂直拉伸变换，仅用于输出。"""
        if not strokes: return []
        new_strokes = copy.deepcopy(strokes)
        stretch_factor = self.stretch_var.get()
        if abs(stretch_factor - 1.0) < 1e-3: return new_strokes
        center_y = self.vectorizer.image_height / 2.0
        for stroke in new_strokes:
            for point in stroke.points:
                point.y = center_y + (point.y - center_y) * stretch_factor
        return new_strokes

    def start_drawing(self):
        if not self.original_strokes:
            messagebox.showinfo("提示", "请先处理一张图片以生成笔画。")
            return
            
        self.update_status("准备开始绘画...")
        
        # 1. 应用变换，生成用于本次绘画的笔画数据
        strokes_for_drawing = self._apply_vertical_stretch(self.original_strokes)
        self.drawer.set_strokes(strokes_for_drawing)
        
        # 2. 设置其他绘图参数
        self.drawer.sensitivity = self.sensitivity_var.get()
        self.drawer.point_delay = self.delay_var.get() / 1000.0
        self.drawer.lift_pen_speed = self.lift_speed_var.get()
        
        # 3. 启动绘图
        self.drawer.start_drawing()
        if self.drawer.drawing_active:
             total_points = sum(len(s.points) for s in strokes_for_drawing)
             self.update_status(f"绘画中...({total_points}个点) 按F10停止。")

    def load_image(self):
        file_path = filedialog.askopenfilename(title="选择图片", filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp")])
        if file_path:
            self.current_image_path = file_path
            self.file_label.config(text=os.path.basename(file_path))
            self.update_status(f"已加载: {os.path.basename(file_path)}")
            self.preview_canvas.delete("all")
            self.original_strokes = []
            self.drawer.set_strokes([])

    def process_image(self):
        if not self.current_image_path:
            messagebox.showwarning("警告", "请先选择一张图片。")
            return
        self.update_status("正在处理图片...")
        self.root.update_idletasks()
        self.vectorizer.binary_threshold = int(self.threshold_var.get())
        self.vectorizer.simplify_epsilon = self.epsilon_var.get()
        threading.Thread(target=self._process_image_thread, daemon=True).start()

    def _process_image_thread(self):
        strokes = self.vectorizer.process_image(self.current_image_path)
        if strokes:
            max_dist = self.max_dist_var.get()
            interpolated_strokes = self.interpolate_strokes(strokes, max_dist)
            self.root.after(0, self.on_processing_done, interpolated_strokes)
        else:
            self.root.after(0, self.on_processing_done, None)
    
    def on_processing_done(self, strokes: Optional[List[DrawingStroke]]):
        if strokes:
            self.original_strokes = strokes
            total_points = sum(len(s.points) for s in self.original_strokes)
            self.update_status(f"处理完成！{len(self.original_strokes)}个笔画，共 {total_points} 个点。")
            self.draw_preview()
        else:
            self.update_status("处理失败，未生成任何笔画。")
            self.preview_canvas.delete("all")
            self.original_strokes = []

    def draw_preview(self):
        """此函数现在总是绘制未经变换的原始笔画"""
        self.preview_canvas.delete("all")
        if not self.original_strokes: return
        self.preview_canvas.after(50, self._redraw_canvas_lines)

    def _redraw_canvas_lines(self):
        self.preview_canvas.delete("all")
        # 重点：使用 self.original_strokes
        if not self.original_strokes: return
        all_points = [p for s in self.original_strokes for p in s.points if s.points]
        if not all_points: return

        min_x = min(p.x for p in all_points)
        max_x = max(p.x for p in all_points)
        min_y = min(p.y for p in all_points)
        max_y = max(p.y for p in all_points)
        img_width = max_x - min_x
        img_height = max_y - min_y
        canvas_width, canvas_height = self.preview_canvas.winfo_width(), self.preview_canvas.winfo_height()

        if canvas_width <= 1 or canvas_height <= 1:
            self.preview_canvas.after(100, self._redraw_canvas_lines)
            return
        
        scale = 1.0
        if img_width > 0 and img_height > 0:
            scale = min((canvas_width - 20) / img_width, (canvas_height - 20) / img_height)

        pad_x = (canvas_width - img_width * scale) / 2
        pad_y = (canvas_height - img_height * scale) / 2

        for stroke in self.original_strokes:
            if len(stroke.points) > 1:
                scaled_points = [( (p.x - min_x) * scale + pad_x, (p.y - min_y) * scale + pad_y ) for p in stroke.points]
                self.preview_canvas.create_line(scaled_points, fill='black', width=1.5)

    def stop_drawing(self):
        self.drawer.stop_drawing()
        self.update_status("绘画已强制停止。")

    def update_status(self, text: str):
        self.status_label.config(text=f"状态: {text}")

    def on_closing(self):
        if self.drawer.drawing_active:
            if messagebox.askokcancel("退出", "绘画正在进行中，确定要退出吗？"):
                self.stop_drawing()
                self.root.destroy()
        else:
            try: keyboard.unhook_all_hotkeys()
            except Exception: pass
            self.root.destroy()

def main():
    root = tk.Tk()
    app = DrawingToolGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
