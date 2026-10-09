using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

public class TrayApp : Form
{
    private NotifyIcon tray;
    private Process child;
    private string exeDir;
    private string appPy;
    private bool nativeMode = false;
    private string url = "http://127.0.0.1:7860";
    private System.Windows.Forms.Timer watch;

    [STAThread]
    static int Main()
    {
        // 任何启动阶段的异常都必须「可见」。
        // 教训（v4.0.4）：OnLoad 里的异常原来完全没有捕获，窗体还没显示、
        // 托盘还没建立，进程就静默死掉 —— 用户看到的就是「双击没反应」。
        AppDomain.CurrentDomain.UnhandledException +=
            new UnhandledExceptionEventHandler(OnUnhandled);
        Application.ThreadException +=
            new ThreadExceptionEventHandler(OnThreadException);
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try
        {
            Application.Run(new TrayApp());
        }
        catch (Exception ex)
        {
            Fatal("Application.Run 抛出异常", ex);
            return 1;
        }
        return 0;
    }

    private static string ExeDirStatic()
    {
        try { return Path.GetDirectoryName(Application.ExecutablePath); }
        catch { return null; }
    }

    /// <summary>
    /// 把启动期异常写到 launcher.log，并弹出最简提示。
    /// 写日志本身失败时也不能再抛异常（否则又变成静默退出）。
    /// </summary>
    private static void Fatal(string where, Exception ex)
    {
        string detail = where + "\r\n" + (ex == null ? "(无异常对象)" : ex.ToString());
        string dir = ExeDirStatic();
        bool logged = false;
        if (dir != null)
        {
            try
            {
                File.AppendAllText(Path.Combine(dir, "launcher.log"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + detail + "\r\n\r\n",
                    new UTF8Encoding(false));
                logged = true;
            }
            catch { }
        }
        try
        {
            MessageBox.Show("程序启动失败。\r\n\r\n" + detail +
                (logged ? "\r\n\r\n详细信息已写入 launcher.log" : ""),
                "TK 素材工坊", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        catch { }
    }

    private static void OnUnhandled(object sender, UnhandledExceptionEventArgs e)
    {
        Fatal("未处理异常（AppDomain）", e.ExceptionObject as Exception);
    }

    private static void OnThreadException(object sender, ThreadExceptionEventArgs e)
    {
        Fatal("未处理异常（UI 线程）", e.Exception);
    }

    public TrayApp()
    {
        this.WindowState = FormWindowState.Minimized;
        this.ShowInTaskbar = false;
        this.Load += OnLoad;
    }

    private void OnLoad(object sender, EventArgs e)
    {
        // 整个启动流程包在 try 里：宁可弹出错误框，也不能静默死掉。
        try
        {
            OnLoadCore();
        }
        catch (Exception ex)
        {
            Fatal("OnLoad 启动流程异常", ex);
            try { Application.Exit(); } catch { }
        }
    }

    private void OnLoadCore()
    {
        this.Hide();
        exeDir = Path.GetDirectoryName(Application.ExecutablePath);
        FixVenv(exeDir);

        // v4.0：桌面版唯一入口 desktop.py（自带 WebView2 原生窗口，不开浏览器）。
        appPy = Path.Combine(exeDir, "desktop.py");
        nativeMode = true;
        if (!File.Exists(appPy))
        {
            SayError("未找到 desktop.py，请确认程序目录完整。\r\n\r\n" + appPy);
            return;
        }
        string python = FindPython(exeDir);
        if (python == null)
        {
            SayError("未找到可用的 Python 运行环境，请确认 venv 目录完整。\r\n\r\n" + exeDir);
            return;
        }
        StartServer(python);
        SetupTray();
        if (!nativeMode)
        {
            // 只有回退到 Web 模式时才需要打开浏览器
            TryOpenBrowser();
        }
    }

    /// <summary>把错误同时写到 launcher.log 并弹出提示。</summary>
    private static void SayError(string message)
    {
        string dir = ExeDirStatic();
        if (dir != null)
        {
            try
            {
                File.AppendAllText(Path.Combine(dir, "launcher.log"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + "\r\n\r\n",
                    new UTF8Encoding(false));
            }
            catch { }
        }
        try
        {
            MessageBox.Show(message, "TK 素材工坊",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        catch { }
        try { Application.Exit(); } catch { }
    }

    private void StartServer(string python)
    {
        string pyDir = Path.GetDirectoryName(python);
        string pythonw = Path.Combine(pyDir, "pythonw.exe");
        string exe = File.Exists(pythonw) ? pythonw : python;
        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = exe;
        psi.Arguments = "\"" + appPy + "\"";
        psi.WorkingDirectory = exeDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        // v4.0.4：这里原来设置了 psi.EnvironmentVariables["TK_TRAY_HOSTED"]=...，
        // 在「环境里同时存在 NO_PROXY 和 no_proxy」的机器上，.NET 的
        // EnvironmentVariables 是大小写不敏感字典，赋值时抛
        //   ArgumentException: 已添加项。字典中的关键字:"NO_PROXY"所添加的关键字:"no_proxy"
        // 于是启动器什么也没启动就静默死掉。
        // 那两个变量只为 v3 的 Python 托盘服务，托盘早已由本启动器负责，
        // 全项目已无任何代码读取它们 —— 直接去掉，改成完整继承父进程环境。
        CreateKillOnCloseJob();
        child = Process.Start(psi);
        AssignChildToJob();

        watch = new System.Windows.Forms.Timer();
        watch.Interval = 2000;
        watch.Tick += OnWatch;
        watch.Start();
    }

    private void OnWatch(object sender, EventArgs e)
    {
        try
        {
            if (child != null && child.HasExited)
            {
                watch.Stop();
                try { if (tray != null) tray.Visible = false; } catch { }
                // v4.0：子进程异常退出时给用户一个可见提示，
                // 原来这里直接 Application.Exit()，表现为「双击没反应」。
                int code = -1;
                try { code = child.ExitCode; } catch { }
                if (code != 0)
                {
                    SayError("程序启动失败（退出码 " + code + "）。\r\n" +
                        "请查看日志：" + Path.Combine(exeDir, "app.log"));
                    return;
                }
                Application.Exit();
            }
        }
        catch (Exception ex)
        {
            Fatal("OnWatch 异常", ex);
        }
    }

    private void SetupTray()
    {
        tray = new NotifyIcon();
        tray.Icon = LoadAppIcon(exeDir);
        tray.Text = "TK 素材工坊（运行中）";
        ContextMenuStrip menu = new ContextMenuStrip();
        menu.Items.Add("打开界面", null, (s, ev) => OpenBrowser());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("退出程序", null, (s, ev) => ExitApp());
        tray.ContextMenuStrip = menu;
        tray.DoubleClick += (s, ev) => OpenBrowser();
        tray.Visible = true;
    }

    /// 读取 assets\icon.ico 作为托盘与窗口图标；缺失时回退到系统默认图标。
    private static Icon LoadAppIcon(string exeDir)
    {
        try
        {
            string p = Path.Combine(exeDir, "assets", "icon.ico");
            if (File.Exists(p)) return new Icon(p, new Size(32, 32));
        }
        catch { }
        return SystemIcons.Application;
    }

    private void TryOpenBrowser()
    {
        Thread t = new Thread(() =>
        {
            for (int i = 0; i < 80; i++)
            {
                if (child == null || child.HasExited) return;
                try
                {
                    using (TcpClient c = new TcpClient())
                    {
                        c.Connect("127.0.0.1", 7860);
                        OpenBrowser();
                        return;
                    }
                }
                catch { }
                Thread.Sleep(500);
            }
            try { if (tray != null) tray.ShowBalloonTip(5000, "TK 素材工坊", "服务启动失败，请查看 logs 目录下的日志。", ToolTipIcon.Error); } catch { }
        });
        t.IsBackground = true;
        t.Start();
    }

    private void OpenBrowser()
    {
        try { Process.Start(url); } catch { }
    }

    private void ExitApp()
    {
        if (child != null && !child.HasExited)
        {
            KillTree(child.Id);
        }
        try { if (tray != null) tray.Visible = false; } catch { }
        Application.Exit();
    }

    private static void KillTree(int pid)
    {
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = "taskkill.exe";
            psi.Arguments = "/T /F /PID " + pid;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            using (Process p = Process.Start(psi))
            {
                p.WaitForExit(10000);
            }
        }
        catch { }
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (e.CloseReason == CloseReason.UserClosing)
        {
            e.Cancel = true;
            this.Hide();
            return;
        }
        base.OnFormClosing(e);
    }


    private static void FixVenv(string exeDir)
    {
        try
        {
            string cfg = Path.Combine(exeDir, "venv", "pyvenv.cfg");
            if (!File.Exists(cfg)) return;
            string home = Path.Combine(exeDir, "python-runtime");
            string[] lines = File.ReadAllLines(cfg, Encoding.UTF8);
            bool found = false;
            for (int i = 0; i < lines.Length; i++)
            {
                if (lines[i].StartsWith("home =", StringComparison.OrdinalIgnoreCase))
                {
                    lines[i] = "home = " + home;
                    found = true;
                    break;
                }
            }
            if (!found)
            {
                string[] nl = new string[lines.Length + 1];
                Array.Copy(lines, nl, lines.Length);
                nl[lines.Length] = "home = " + home;
                lines = nl;
            }
            File.WriteAllLines(cfg, lines, new UTF8Encoding(false));
        }
        catch { }
    }

    // ---------- Python 查找 ----------

    private static string FindPython(string exeDir)
    {
        string[] candidates = new string[] {
            Path.Combine(exeDir, "venv", "Scripts", "python.exe"),
            ReadPathFile(exeDir),
            FindOnPath("python.exe"),
        };
        foreach (string c in candidates)
        {
            if (string.IsNullOrEmpty(c) || !File.Exists(c)) continue;
            if (HasRuntime(c)) return c;
        }
        return null;
    }

    private static string ReadPathFile(string exeDir)
    {
        try
        {
            string f = Path.Combine(exeDir, "python_path.txt");
            if (!File.Exists(f)) return null;
            string s = File.ReadAllText(f, Encoding.UTF8).Trim();
            if (s.Length == 0) return null;
            if (Path.IsPathRooted(s)) return s;
            return Path.Combine(exeDir, s);
        }
        catch { return null; }
    }

    /// <summary>
    /// 检查该解释器是否有运行所需依赖。
    /// v4.0：桌面模式需要 webview（pywebview）；Web 回退模式需要 gradio。
    /// 原来只探测 gradio，且超时 20 秒 —— 换机器或换入口时会误判。
    /// </summary>
    private static bool HasRuntime(string python)
    {
        string probe = "import importlib.util as u,sys;" +
            "sys.exit(0 if (u.find_spec('webview') or u.find_spec('gradio')) else 1)";
        return RunProbe(python, probe);
    }

    private static bool RunProbe(string python, string code)
    {
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo(python, "-c \"" + code + "\"");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            using (Process p = Process.Start(psi))
            {
                if (!p.WaitForExit(20000)) { try { p.Kill(); } catch { } return false; }
                return p.ExitCode == 0;
            }
        }
        catch { return false; }
    }

    private static string FindOnPath(string name)
    {
        string pathVar = Environment.GetEnvironmentVariable("PATH");
        if (pathVar == null) return null;
        foreach (string dir in pathVar.Split(';'))
        {
            string d = (dir ?? "").Trim();
            if (d.Length == 0) continue;
            try
            {
                string full = Path.Combine(d, name);
                if (File.Exists(full)) return full;
            }
            catch { }
        }
        return null;
    }

    // ================= Windows 作业对象（v4.0）=================
    // 把 Python 子进程绑进一个 KILL_ON_JOB_CLOSE 的作业对象。
    // 这样即使用户在任务管理器里强杀启动器，或启动器自己崩了，
    // 子进程也会被内核连带结束 —— 从根上避免：
    //   ① 残留 pythonw 占着端口 → 下次启动报「Cannot find empty port」
    //   ② 「关不掉的后台进程」
    // 实测本机确实出现过 v2.1 遗留进程占着 7860 的情况。

    private IntPtr jobHandle = IntPtr.Zero;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

    [DllImport("kernel32.dll")]
    private static extern bool SetInformationJobObject(IntPtr hJob, int infoClass,
        IntPtr lpJobObjectInfo, uint cbJobObjectInfoLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

    private void CreateKillOnCloseJob()
    {
        try
        {
            jobHandle = CreateJobObject(IntPtr.Zero, null);
            if (jobHandle == IntPtr.Zero) { jobHandle = IntPtr.Zero; return; }
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION info =
                new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            int len = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr p = Marshal.AllocHGlobal(len);
            try
            {
                Marshal.StructureToPtr(info, p, false);
                if (!SetInformationJobObject(jobHandle,
                        JobObjectExtendedLimitInformation, p, (uint)len))
                {
                    // 设置失败就放弃使用作业对象，不影响正常流程
                    jobHandle = IntPtr.Zero;
                }
            }
            finally { Marshal.FreeHGlobal(p); }
        }
        catch { jobHandle = IntPtr.Zero; }
    }

    private void AssignChildToJob()
    {
        if (jobHandle == IntPtr.Zero || child == null) return;
        try { AssignProcessToJobObject(jobHandle, child.Handle); }
        catch { }
    }
}

