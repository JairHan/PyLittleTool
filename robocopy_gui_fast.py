import os
import sys
import locale
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


class RoboCopyFastGUI(tk.Tk):
    PRESETS = {
        "均衡推荐：移动硬盘通用": {
            "threads": 16,
            "unbuffered": True,
            "quiet": True,
            "eta": False,
            "description": "适合大多数移动硬盘。减少屏幕输出，速度和可观察性比较均衡。",
        },
        "机械移动硬盘：稳妥": {
            "threads": 8,
            "unbuffered": True,
            "quiet": True,
            "eta": False,
            "description": "适合 2.5 寸机械移动硬盘。线程太高可能随机读写变多，反而变慢。",
        },
        "移动 SSD：高速": {
            "threads": 32,
            "unbuffered": True,
            "quiet": True,
            "eta": False,
            "description": "适合 USB 3.x 移动固态硬盘。线程数可以适当提高。",
        },
        "很多小文件：少输出": {
            "threads": 32,
            "unbuffered": False,
            "quiet": True,
            "eta": False,
            "description": "适合照片、代码、资料、缓存等海量小文件。关闭 /J，减少输出最重要。",
        },
        "单个/少量超大文件": {
            "threads": 4,
            "unbuffered": True,
            "quiet": True,
            "eta": False,
            "description": "适合 ISO、虚拟机镜像、压缩包、视频素材。/J 更有意义，线程数不用太高。",
        },
        "详细输出：方便排查": {
            "threads": 16,
            "unbuffered": True,
            "quiet": False,
            "eta": True,
            "description": "会显示更多输出，便于观察，但大量文件时可能略慢。",
        },
    }

    def __init__(self):
        super().__init__()

        self.title("Robocopy 高速复制工具 - 优化版")
        self.geometry("980x720")
        self.minsize(880, 620)

        self.process = None
        self.worker_thread = None
        self.output_queue = queue.Queue()

        self.source_var = tk.StringVar()
        self.dest_var = tk.StringVar()
        self.log_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "copy_log.txt"))

        self.preset_var = tk.StringVar(value="均衡推荐：移动硬盘通用")
        self.thread_var = tk.IntVar(value=16)
        self.retry_var = tk.IntVar(value=2)
        self.wait_var = tk.IntVar(value=2)

        self.copy_empty_var = tk.BooleanVar(value=True)
        self.use_unbuffered_var = tk.BooleanVar(value=True)
        self.use_log_var = tk.BooleanVar(value=True)
        self.append_log_var = tk.BooleanVar(value=True)
        self.keep_dest_folder_var = tk.BooleanVar(value=True)

        self.quiet_output_var = tk.BooleanVar(value=True)
        self.show_eta_var = tk.BooleanVar(value=False)
        self.exclude_junctions_var = tk.BooleanVar(value=True)
        self.no_console_tee_var = tk.BooleanVar(value=False)

        self.status_var = tk.StringVar(value="请选择源文件夹和目标位置。")
        self.command_preview_var = tk.StringVar(value="")
        self.preset_desc_var = tk.StringVar(value=self.PRESETS[self.preset_var.get()]["description"])

        self._build_ui()
        self.apply_preset()
        self._poll_output_queue()

    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(root, text="Robocopy 高速复制工具 - 优化版", font=("Microsoft YaHei UI", 16, "bold"))
        title.pack(anchor=tk.W)

        desc = ttk.Label(
            root,
            text="适合复制大文件 / 大量文件到移动硬盘。新增快速模式：减少输出开销、按硬盘类型自动调整线程数。",
            foreground="#555555",
        )
        desc.pack(anchor=tk.W, pady=(4, 12))

        form = ttk.LabelFrame(root, text="复制路径", padding=10)
        form.pack(fill=tk.X)

        self._path_row(form, "源文件夹：", self.source_var, self.choose_source, row=0)
        self._path_row(form, "目标位置：", self.dest_var, self.choose_dest, row=1)
        self._path_row(form, "日志文件：", self.log_var, self.choose_log, row=2)

        preset_frame = ttk.LabelFrame(root, text="性能模式", padding=10)
        preset_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(preset_frame, text="模式：").grid(row=0, column=0, sticky=tk.W, pady=4)
        preset_box = ttk.Combobox(
            preset_frame,
            textvariable=self.preset_var,
            values=list(self.PRESETS.keys()),
            state="readonly",
            width=30,
        )
        preset_box.grid(row=0, column=1, sticky=tk.W, padx=(6, 12), pady=4)
        preset_box.bind("<<ComboboxSelected>>", lambda _event: self.apply_preset())

        ttk.Label(preset_frame, textvariable=self.preset_desc_var, foreground="#666666").grid(
            row=0, column=2, sticky=tk.W, pady=4
        )
        preset_frame.columnconfigure(2, weight=1)

        options = ttk.LabelFrame(root, text="复制参数", padding=10)
        options.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(options, text="线程数 /MT：").grid(row=0, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        thread_box = ttk.Spinbox(options, from_=1, to=128, textvariable=self.thread_var, width=8, command=self.refresh_command_preview)
        thread_box.grid(row=0, column=1, sticky=tk.W, pady=4)

        ttk.Label(options, text="失败重试 /R：").grid(row=0, column=2, sticky=tk.W, padx=(24, 6), pady=4)
        retry_box = ttk.Spinbox(options, from_=0, to=100, textvariable=self.retry_var, width=8, command=self.refresh_command_preview)
        retry_box.grid(row=0, column=3, sticky=tk.W, pady=4)

        ttk.Label(options, text="重试等待秒 /W：").grid(row=0, column=4, sticky=tk.W, padx=(24, 6), pady=4)
        wait_box = ttk.Spinbox(options, from_=0, to=3600, textvariable=self.wait_var, width=8, command=self.refresh_command_preview)
        wait_box.grid(row=0, column=5, sticky=tk.W, pady=4)

        checks1 = ttk.Frame(options)
        checks1.grid(row=1, column=0, columnspan=6, sticky=tk.W, pady=(8, 2))

        self._check(checks1, "复制空文件夹 /E", self.copy_empty_var)
        self._check(checks1, "大文件无缓冲 /J", self.use_unbuffered_var)
        self._check(checks1, "启用日志 /LOG", self.use_log_var)
        self._check(checks1, "追加日志 /LOG+", self.append_log_var)

        checks2 = ttk.Frame(options)
        checks2.grid(row=2, column=0, columnspan=6, sticky=tk.W, pady=(4, 2))

        self._check(checks2, "目标中自动创建源文件夹同名目录", self.keep_dest_folder_var)
        self._check(checks2, "快速少输出 /NP /NFL /NDL", self.quiet_output_var)
        self._check(checks2, "显示预计时间 /ETA", self.show_eta_var)
        self._check(checks2, "跳过链接目录 /XJ", self.exclude_junctions_var)
        self._check(checks2, "只写日志，不同步刷屏", self.no_console_tee_var)

        hint = ttk.Label(
            options,
            text="速度建议：很多小文件优先打开“快速少输出”；单个大文件用 /J；不要使用 /MIR，避免误删目标盘文件。",
            foreground="#666666",
        )
        hint.grid(row=3, column=0, columnspan=6, sticky=tk.W, pady=(8, 0))

        preview_box = ttk.LabelFrame(root, text="命令预览", padding=10)
        preview_box.pack(fill=tk.X, pady=(10, 0))

        self.preview_entry = ttk.Entry(preview_box, textvariable=self.command_preview_var)
        self.preview_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(preview_box, text="刷新预览", command=self.refresh_command_preview).pack(side=tk.LEFT, padx=(8, 0))

        actions = ttk.Frame(root)
        actions.pack(fill=tk.X, pady=(10, 0))

        self.start_btn = ttk.Button(actions, text="开始复制", command=self.start_copy)
        self.start_btn.pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(actions, text="停止复制", command=self.stop_copy, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Button(actions, text="清空输出", command=self.clear_output).pack(side=tk.LEFT, padx=(8, 0))

        status = ttk.Label(actions, textvariable=self.status_var, foreground="#333333")
        status.pack(side=tk.LEFT, padx=(16, 0))

        output_frame = ttk.LabelFrame(root, text="复制输出", padding=8)
        output_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        self.output_text = tk.Text(output_frame, wrap=tk.NONE, height=18)
        self.output_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        y_scroll = ttk.Scrollbar(output_frame, orient=tk.VERTICAL, command=self.output_text.yview)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.output_text.configure(yscrollcommand=y_scroll.set)

        x_scroll = ttk.Scrollbar(root, orient=tk.HORIZONTAL, command=self.output_text.xview)
        x_scroll.pack(fill=tk.X)
        self.output_text.configure(xscrollcommand=x_scroll.set)

    def _check(self, parent, text, variable):
        cb = ttk.Checkbutton(parent, text=text, variable=variable, command=self.refresh_command_preview)
        cb.pack(side=tk.LEFT, padx=(0, 18))
        return cb

    def _path_row(self, parent, label, variable, command, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=5)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky=tk.EW, padx=8, pady=5)
        ttk.Button(parent, text="选择", command=command).grid(row=row, column=2, sticky=tk.E, pady=5)
        parent.columnconfigure(1, weight=1)

    def apply_preset(self):
        preset = self.PRESETS[self.preset_var.get()]
        self.thread_var.set(preset["threads"])
        self.use_unbuffered_var.set(preset["unbuffered"])
        self.quiet_output_var.set(preset["quiet"])
        self.show_eta_var.set(preset["eta"])
        self.preset_desc_var.set(preset["description"])
        self.refresh_command_preview()

    def choose_source(self):
        path = filedialog.askdirectory(title="选择源文件夹")
        if path:
            self.source_var.set(path)
            self.refresh_command_preview()

    def choose_dest(self):
        path = filedialog.askdirectory(title="选择目标位置，例如 I:\\ 或 I:\\01_研究资料")
        if path:
            self.dest_var.set(path)
            self.refresh_command_preview()

    def choose_log(self):
        path = filedialog.asksaveasfilename(
            title="选择日志文件",
            initialfile="copy_log.txt",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.log_var.set(path)
            self.refresh_command_preview()

    def build_command(self):
        source = self.source_var.get().strip()
        dest = self.dest_var.get().strip()

        if self.keep_dest_folder_var.get() and source and dest:
            folder_name = os.path.basename(os.path.normpath(source))
            dest = os.path.join(dest, folder_name)

        cmd = ["robocopy", source, dest]

        if self.copy_empty_var.get():
            cmd.append("/E")
        else:
            cmd.append("/S")

        thread_count = max(1, min(int(self.thread_var.get()), 128))
        retry_count = max(0, int(self.retry_var.get()))
        wait_count = max(0, int(self.wait_var.get()))

        cmd.extend([
            f"/MT:{thread_count}",
            f"/R:{retry_count}",
            f"/W:{wait_count}",
            "/COPY:DAT",
            "/DCOPY:DAT",
        ])

        if self.use_unbuffered_var.get():
            cmd.append("/J")

        if self.exclude_junctions_var.get():
            cmd.append("/XJ")

        if self.show_eta_var.get():
            cmd.append("/ETA")

        if self.quiet_output_var.get():
            cmd.extend(["/NP", "/NFL", "/NDL"])

        if self.use_log_var.get():
            log_path = self.log_var.get().strip()
            if log_path:
                if self.append_log_var.get():
                    cmd.append(f"/LOG+:{log_path}")
                else:
                    cmd.append(f"/LOG:{log_path}")
                if not self.no_console_tee_var.get():
                    cmd.append("/TEE")

        return cmd

    def refresh_command_preview(self):
        try:
            cmd = self.build_command()
            self.command_preview_var.set(" ".join(self.quote_arg(x) for x in cmd if x))
        except Exception:
            pass

    @staticmethod
    def quote_arg(arg):
        if not arg:
            return '""'
        if " " in arg or "\t" in arg:
            return f'"{arg}"'
        return arg

    def validate_inputs(self):
        source = self.source_var.get().strip()
        dest = self.dest_var.get().strip()

        if not source:
            messagebox.showwarning("缺少源文件夹", "请先选择源文件夹。")
            return False

        if not os.path.isdir(source):
            messagebox.showerror("源文件夹不存在", f"源文件夹不存在：\n{source}")
            return False

        if not dest:
            messagebox.showwarning("缺少目标位置", "请先选择目标位置。")
            return False

        if not os.path.isdir(dest):
            answer = messagebox.askyesno("目标位置不存在", f"目标位置不存在，是否创建？\n{dest}")
            if not answer:
                return False
            try:
                os.makedirs(dest, exist_ok=True)
            except Exception as exc:
                messagebox.showerror("创建目标失败", str(exc))
                return False

        if self.use_log_var.get():
            log_path = self.log_var.get().strip()
            if log_path:
                log_dir = os.path.dirname(log_path)
                if log_dir and not os.path.isdir(log_dir):
                    try:
                        os.makedirs(log_dir, exist_ok=True)
                    except Exception as exc:
                        messagebox.showerror("创建日志目录失败", str(exc))
                        return False

        return True

    def start_copy(self):
        if self.process is not None:
            messagebox.showinfo("正在复制", "当前已经有复制任务在运行。")
            return

        if not self.validate_inputs():
            return

        self.refresh_command_preview()
        cmd = self.build_command()

        self.output_text.insert(tk.END, "\n========== 开始复制 ==========\n")
        self.output_text.insert(tk.END, "命令：\n" + " ".join(self.quote_arg(x) for x in cmd) + "\n\n")
        if self.quiet_output_var.get():
            self.output_text.insert(tk.END, "已启用快速少输出模式：复制过程中窗口输出会变少，详细信息请看日志文件。\n\n")
        self.output_text.see(tk.END)

        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.status_var.set("正在复制...")

        self.worker_thread = threading.Thread(target=self._run_robocopy, args=(cmd,), daemon=True)
        self.worker_thread.start()

    def _run_robocopy(self, cmd):
        encoding = locale.getpreferredencoding(False) or "mbcs"

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding=encoding,
                errors="replace",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform.startswith("win") else 0,
            )

            assert self.process.stdout is not None

            for line in self.process.stdout:
                self.output_queue.put(("line", line))

            return_code = self.process.wait()
            self.output_queue.put(("done", return_code))

        except FileNotFoundError:
            self.output_queue.put(("error", "没有找到 robocopy。请确认你是在 Windows 上运行本程序。"))
        except Exception as exc:
            self.output_queue.put(("error", str(exc)))

    def _poll_output_queue(self):
        try:
            while True:
                kind, payload = self.output_queue.get_nowait()

                if kind == "line":
                    self.output_text.insert(tk.END, payload)
                    self.output_text.see(tk.END)

                elif kind == "done":
                    code = int(payload)
                    self.process = None
                    self.start_btn.configure(state=tk.NORMAL)
                    self.stop_btn.configure(state=tk.DISABLED)

                    if code <= 7:
                        msg = f"复制结束。robocopy 返回码：{code}。0~7 通常表示成功或有少量可接受差异。"
                    else:
                        msg = f"复制可能失败。robocopy 返回码：{code}。8 及以上通常表示错误。"

                    self.status_var.set(msg)
                    self.output_text.insert(tk.END, "\n========== " + msg + " ==========\n")
                    self.output_text.see(tk.END)

                elif kind == "error":
                    self.process = None
                    self.start_btn.configure(state=tk.NORMAL)
                    self.stop_btn.configure(state=tk.DISABLED)
                    self.status_var.set("发生错误。")
                    self.output_text.insert(tk.END, "\n错误：" + str(payload) + "\n")
                    self.output_text.see(tk.END)

        except queue.Empty:
            pass

        self.after(150, self._poll_output_queue)

    def stop_copy(self):
        if self.process is None:
            return

        answer = messagebox.askyesno("确认停止", "确定要停止当前复制任务吗？")
        if not answer:
            return

        try:
            pid = self.process.pid

            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.process.terminate()

            self.output_text.insert(tk.END, "\n用户已请求停止复制任务。\n")
            self.output_text.see(tk.END)
            self.status_var.set("正在停止...")

        except Exception as exc:
            messagebox.showerror("停止失败", str(exc))

    def clear_output(self):
        self.output_text.delete("1.0", tk.END)


if __name__ == "__main__":
    app = RoboCopyFastGUI()
    app.mainloop()
