# 安装指南 · INSTALL

面向**从本仓库克隆源码**的用户。本仓库只收录源码与文档 —— 虚拟环境、浏览器内核、
模型权重、ffmpeg 都**不入库**（合计 6.9 GB），需要按本文准备。

> 功能说明与界面截图见 [README.md](README.md) ｜ 版本沿革见 [CHANGELOG.md](CHANGELOG.md)

---

## 〇、先选一条路

| 路线 | 适合谁 | 需要准备 | 最终体积 |
|---|---|---|---|
| **A. 从源码装**（本仓库，本文正文） | 想自己跑起来、想改代码 | Python 3.10 | 约 10 GB |
| **B. 做成便携完整版** | 想拷给别人 / 换电脑不装环境 | **先做完 A**，再打包 | 约 6.9 GB |

> ⚠️ **没有"下载即用"的完整版**。完整版压缩包约 6.9 GB，**不在本仓库**
> （GitHub 单文件上限 2 GB），只能按路线 A 把环境装出来之后，
> 用 `venv\Scripts\python.exe scripts\package.py` 自己打出来。
> 仓库根目录那个 `TK素材工坊.exe`（109 KB）**只是托盘启动器**，
> 单独下载它没有任何用 —— 它需要同目录下已经装好的 `venv\`。

**只想用某一部分？** 不必装全套，见 [第四节 · 按需安装](#四按需安装只想用某一部分)。

---

## 一、环境要求

| 项目 | 要求 | 说明 |
|---|---|---|
| 操作系统 | **Windows 10 / 11（64 位）** | 桌面外壳基于 WebView2，仅支持 Windows |
| Python | **3.10.x（64 位）** | 与开发环境一致（3.10.11）。**不建议 3.12+** —— basicsr / gfpgan 在新版 Python 上编译易出问题 |
| 磁盘空间 | **约 10 GB** | torch(CUDA)≈3 GB ＋ 模型≈1.4 GB ＋ 依赖≈2 GB ＋ 预留 |
| 网络 | 能访问 GitHub / PyPI | 国内可用镜像加速，见各步骤注释 |
| 显卡（可选） | NVIDIA + 支持 CUDA 12.8 的驱动 | 无显卡走 CPU 也能跑，推理慢 5~10 倍 |
| WebView2 运行时 | Win11 自带；Win10 旧版可能需补装 | [下载地址](https://developer.microsoft.com/microsoft-edge/webview2/) |

> 程序**全程只监听 `127.0.0.1`**，不开局域网时外部无法访问，数据不出本机。

---

## 二、从源码安装

以下命令均在**程序根目录**（即 `desktop.py` 所在目录）执行。
Windows 命令提示符（cmd）里用 `^` 续行；PowerShell 里改行尾为反引号 `` ` ``。

### 第 1 步 · 安装 Python 3.10

到 [python.org](https://www.python.org/downloads/release/python-31011/) 下载
**Windows installer (64-bit)** 安装，安装时**勾选 `Add python.exe to PATH`**。

验证：

```bat
python -V
:: 应输出 Python 3.10.x
```

> 如果机器上已有多个 Python，后面所有命令把 `python` 换成 `py -3.10` 更保险。

### 第 2 步 · 获取源码

```bat
git clone https://github.com/chenquaerere/tk-studio.git
cd tk-studio
```

> 也可以直接在 GitHub 页面点 **Code → Download ZIP** 下载后解压（无需装 git）。
> 解压路径**建议不含空格与中文**，例如 `D:\TKStudio`。

### 第 3 步 · 创建虚拟环境

```bat
python -m venv venv
```

之后所有命令都用 `venv\Scripts\python.exe` 调用，不会污染系统 Python。

### 第 4 步 · 先装 PyTorch ★ 顺序很重要

**必须先装 torch**。因为 `basicsr` / `gfpgan` / `realesrgan` 都声明了 `torch>=1.7`，
如果先装依赖清单，pip 会顺手从 PyPI 拉一个 CPU 版 torch 下来，之后你再装 CUDA 版
等于白下 3 GB。先装好 torch，pip 发现已满足要求就会跳过。

**A. GPU 版**（有 NVIDIA 显卡；RTX 50 系 Blackwell 必须用 CUDA 12.8）

```bat
venv\Scripts\python.exe -m pip install ^
    torch==2.11.0+cu128 torchvision==0.26.0+cu128 ^
    --index-url https://download.pytorch.org/whl/cu128
```

**B. CPU 版**（没有 NVIDIA 显卡；体积小很多，推理慢 5~10 倍）

```bat
venv\Scripts\python.exe -m pip install ^
    torch==2.11.0 torchvision==0.26.0 ^
    --index-url https://download.pytorch.org/whl/cpu
```

> ⚠️ 这两条命令**不要**加国内 PyPI 镜像参数 —— PyTorch 官方索引才有 CUDA 版。
> 下载慢的话可以在同一条命令后再追加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`
> 作为额外索引，但 `--index-url` 必须保持为 PyTorch 官方。

验证装的是不是 GPU 版：

```bat
venv\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available())"
:: GPU 版应输出：2.11.0+cu128 True
```

### 第 5 步 · 安装其余依赖

```bat
venv\Scripts\python.exe -m pip install -r requirements.txt
```

国内网络慢可以加镜像：

```bat
venv\Scripts\python.exe -m pip install -r requirements.txt ^
    -i https://pypi.tuna.tsinghua.edu.cn/simple
```

装的是一份**锁定版本**的清单（与开发机实测环境一致），关键项：

| 组 | 包 |
|---|---|
| 本地 Web 服务 | fastapi · uvicorn · websockets · aiofiles · **python-multipart** |
| 桌面外壳 | pywebview · pythonnet |
| 链接采集 | playwright |
| 图像与导出 | pillow · pillow_heif · numpy · opencv-python · openpyxl · tqdm |
| 时区数据 | **tzdata**（Windows 上 `zoneinfo` 必需，缺了采集直接报错） |
| 超分内核 | basicsr · gfpgan · facexlib · realesrgan |

> `requirements.txt` 里**不含** torch / torchvision —— 已经在第 4 步装好了。

### 第 6 步 · 安装浏览器内核（采集功能需要）

采集靠 Playwright 驱动真实 Chromium。内核**不在仓库里**，需要单独下载（约 150 MB）：

```bat
venv\Scripts\python.exe -m playwright install chromium
```

默认装到 `%LOCALAPPDATA%\ms-playwright`，程序会自动找到它。

> **想做成便携版？** 把内核装进程序目录，随文件夹一起拷走即可用：
> 先设 `set PLAYWRIGHT_BROWSERS_PATH=%CD%\runtime\ms-playwright` 再执行上面的命令。
> 程序启动时若发现 `runtime\ms-playwright` 存在且非空，就会优先使用它。

### 第 7 步 · 模型权重（高清修复功能需要）

**只做链接采集 / 去水印 → 跳过本步**（这两块不需要任何模型）。

模型放在 `models\`，人脸修复的辅助权重放在 `gfpgan\weights\`（两个目录程序都会自动创建）。
三种方式任选，**方式 1 最省事**。

#### 方式 1 · 什么都不做 —— 程序会自己下载 ★

程序在**第一次用到某个模型时**自动把它下下来（多源回退、每个源重试 2 次、`.part` 断点重来），
界面上能看到进度。触发点例如：在「高清修复」页选了一个还没下过的模型，
或勾上了「人脸修复」。所以**本步可以整段跳过**。

#### 方式 2 · 用脚本提前下好（慢网 / 离线 / 打包便携版前推荐）

```bat
:: 先看清单与当前状态（不下载）
venv\Scripts\python.exe scripts\fetch_models.py --list

:: 最小可用：默认模型 1 个，约 5 MB
venv\Scripts\python.exe scripts\fetch_models.py --min

:: 全部「已接入」模型：5 个，约 484 MB
venv\Scripts\python.exe scripts\fetch_models.py

:: 连可选模型一起（9 个），并备好人脸修复辅助权重（+ 约 186 MB）
venv\Scripts\python.exe scripts\fetch_models.py --all --face

:: 只下某一个 / 走代理下载
venv\Scripts\python.exe scripts\fetch_models.py --key GFPGANv1.4
venv\Scripts\python.exe scripts\fetch_models.py --proxy http://127.0.0.1:11304
```

脚本用的是**程序自身那份下载注册表**（`_app\watermark\core\engines\models.py`），
与程序内自动下载同源。已就位的会跳过，失败的会重试 —— 中断后重跑同一命令即可续上。
只写 `models\` 与 `gfpgan\weights\`，不碰任何用户数据。

#### 方式 3 · 手动下载

**最小可用**只要一个文件：

| 放入 `models\` | 体积 | 说明 |
|---|---|---|
| **`realesr-general-x4v3.pth`** | 4.7 MB | **默认模型**，通用快速 —— 装了它高清修复就能跑 |

想要更好效果再补：

| 文件 | 体积 | 说明 |
|---|---|---|
| `RealESRGAN_x4plus.pth` | 63.9 MB | 通用高质量 |
| `RealESRGAN_x2plus.pth` | 64.0 MB | 2 倍放大 |
| `RealESRGAN_x4plus_anime_6B.pth` | 17.1 MB | 动漫专用 |
| `realesr-animevideov3.pth` | 2.4 MB | 动漫视频专用 |
| `4x-UltraSharp.pth` | 63.9 MB | 锐化风格 |
| `GFPGANv1.4.pth` | 332.5 MB | 人脸修复（开启「人脸修复」必需）|
| `codeformer.pth` | 359.2 MB | 人脸修复备选（未接入界面）|
| `RestoreFormer.pth` | 277.3 MB | 人脸修复备选（未接入界面）|

下载来源：
- Real-ESRGAN 系列 → <https://github.com/xinntao/Real-ESRGAN/releases>
- GFPGAN 系列 → <https://github.com/TencentARC/GFPGAN/releases>

开启「人脸修复」还需要放入 `gfpgan\weights\`（需自己新建）：

| 文件 | 体积 | 说明 |
|---|---|---|
| `detection_Resnet50_Final.pth` | 104.4 MB | 人脸检测 —— 来自 [facexlib v0.1.0](https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth) |
| `parsing_parsenet.pth` | 81.4 MB | 人脸解析 —— 来自 [facexlib v0.2.2](https://github.com/xinntao/facexlib/releases/download/v0.2.2/parsing_parsenet.pth) |

> 程序内的「模型中心 → 模型库」可以看到每个模型的**接入状态、体积与显存占用**，
> 但它只做展示，**没有下载按钮**（与合并前的原程序一致）——
> 下载走方式 1（自动）或方式 2（脚本）。
> 服务接口也可直接调用：`POST /api/models/download`，body `{"key": null}` 下载全部缺失项，
> 单个模型把 `null` 换成模型 key（如 `"realesr-general-x4v3"`）。

### 第 8 步 · 准备 ffmpeg（视频处理需要）

视频高清修复、转 GIF、抽帧都依赖 ffmpeg。放入 `tools\ffmpeg\bin\`：

```bat
:: 目标结构
tools\ffmpeg\bin\ffmpeg.exe
tools\ffmpeg\bin\ffprobe.exe
```

下载来源（任选）：
- <https://www.gyan.dev/ffmpeg/builds/>（选 `ffmpeg-release-essentials.zip`）
- <https://ffmpeg.org/download.html>

解压后把 `bin\` 里的两个 exe 复制过来即可。
**也可以不装** —— 只要把 ffmpeg 加进系统 `PATH`，程序同样能找到。

> 只做链接采集 → 本步可跳过。

### 第 9 步 · 配置文件（可选）

程序**首次启动会自动生成** `config.json`，一般不用管。

想预先设定就复制模板：

```bat
copy config.example.json config.json
```

常见要改的键（`collector` 分区）：

| 键 | 默认 | 说明 |
|---|---|---|
| `proxy_enabled` / `proxy_host` / `proxy_port` | `false` / 空 | 采集 TikTok 通常需要代理 |
| `default_collect_count` | `4` | 每个账号采集多少条 |
| `timezone` | `Asia/Shanghai` | 影响「今天 / 昨天」的判定 |
| `account_interval_seconds` | `4.0` | 账号之间的停顿，防限流 |
| `browser_headless` | — | 是否无头运行浏览器 |

`watermark.output_root` 是去水印/高清的输出目录，默认 `downloads`（相对程序根）。

> `config.json` 含本机路径，**已被 `.gitignore` 排除**，不会误提交。

### 第 10 步 · 启动

```bat
venv\Scripts\python.exe desktop.py
```

程序会拉起本地服务并打开一个**原生窗口**（WebView2，不会打开浏览器）。
服务端口在 7860~7879 之间自动挑选，实际端口写在 `runtime\server.json`。

也可以双击 `TK素材工坊.exe`（已编译的托盘启动器，见第 12 步）。

> **首次启动若界面空白**：看程序根目录的 `app.log` 与 `launcher.log`。

### 第 11 步 · 自检

```bat
venv\Scripts\python.exe scripts\smoke_test.py
```

23 项检查（品牌 / 浏览器内核 / 采集铁律 / 前端 / 服务 / 越权），全绿即可放心使用。

其他可用的验证脚本：

| 命令 | 检查内容 |
|---|---|
| `scripts\fetch_models.py` | 模型权重下载（`--list` / `--min` / `--all` / `--face` / `--proxy`） |
| `scripts\smoke_test.py` | 启动冒烟（23 项） |
| `scripts\verify_bundled_browser.py` | 随身浏览器内核（4 项，含「模拟干净机器」） |
| `scripts\verify_release.py` | 发布目录完整性（9 项） |
| `scripts\e2e_smoke.py` | 端到端（去水印下载 + 高清超分） |
| `pytest _app\collector\tests` | 采集内核单元测试（231 项） |

### 第 12 步 · 编译启动器 exe（可选）

仓库里的 `TK素材工坊.exe` 是 .NET Framework 4.8 的托盘启动器（109 KB，零依赖）。
改了 `scripts\_launcher.cs` 后需要重新编译：

```bat
dotnet build scripts\launcher\_launcher.csproj -c Release
:: 产物：scripts\launcher\bin\Release\TK素材工坊.exe，复制到程序根目录
copy scripts\launcher\bin\Release\TK素材工坊.exe "TK素材工坊.exe"
```

> 旧教程里用 `csc.exe` 编译，但部分环境的安全策略会拦截运行期编译，
> 用 **dotnet SDK** 编译 net48 更稳（本机验证环境 dotnet 10.0.401）。
>
> 不编译也不影响使用 —— 直接跑第 10 步的命令效果一样。

---

## 三、首次使用要做的三件事

装完不等于能用，采集还需要：

1. **登录 TikTok 账号** —— 进「采集工作流 → 聊天链接采集 / 采集任务」，
   点「登录账号」，在打开的浏览器里扫码或输密码。登录态存在 `data\sessions\`，
   之后不用重复登录。
2. **配置代理** —— 进「系统 → 设置 → 采集」，填代理地址并点测试。
   采集 TikTok 在国内网络下通常需要代理。
3. **添加账号并设置地区** —— 进「采集工作流 → 账号管理」，
   点「新增 / 批量导入」把要采集的账号加进去，再「设置地区」做分类，
   这样采集任务页就能按地区批量选号。

---

## 四、按需安装（只想用某一部分）

不必装全套。按下表裁剪：

| 想用的功能 | torch / 模型 | 浏览器内核（第 6 步） | ffmpeg（第 8 步） |
|---|---|---|---|
| **只做链接采集** | 不需要 | **需要** | 不需要 |
| **只做去水印下载** | 不需要 | 不需要 | 建议装（视频处理用） |
| **要用高清修复** | **需要** | 不需要 | **需要** |

对应做法：

- **只要采集**：第 4 步和第 7 步可跳过；`requirements.txt` 里 basicsr / gfpgan /
  facexlib / realesrgan 四行也可以临时注释掉，能省下上 GB。
- **只要去水印**：第 4、6、7 步可跳过。
- **只要高清修复**：第 6 步可跳过。

---

## 五、自带 vs 需自备

| 目录 / 文件 | 谁提供 | 说明 |
|---|---|---|
| `_app\` `scripts\` `docs\` `assets\` `desktop.py` | ✅ 仓库自带 | 全部源码 |
| `venv\` | ❌ 自己建 | 第 3~5 步 |
| `runtime\ms-playwright\` | ❌ 自己装 | 第 6 步（默认也可装到系统目录） |
| `models\` | ❌ 自己下 | 第 7 步（**也可以不管** —— 程序首次用到时会自动下载） |
| `gfpgan\weights\` | ❌ 自己下 | 第 7 步（仅人脸修复需要，同样会自动下载） |
| `tools\ffmpeg\bin\` | ❌ 自己下 | 第 8 步 |
| `python-runtime\` | ❌ 自己建 | 仅完整版自带的内置 Python |
| `config.json` | ⚙️ 自动生成 | 第 9 步（可选模板） |
| `data\` `downloads\` `exports\` `logs\` `runtime\` | ⚙️ 运行期自动创建 | **用户数据，绝不在仓库里** |

---

## 六、常见问题排查

**Q：双击 exe 没有任何反应？**
看程序根目录的 `launcher.log` —— 启动器会把失败原因写进去。若日志为空，
改用命令方式启动 `venv\Scripts\python.exe desktop.py`，错误会直接打在控制台。

**Q：界面能开，但点采集没反应 / 一直转圈？**
三个常见原因，按顺序排查：
1. **代理不通** —— 「设置 → 采集」里点测试；默认走 `http://127.0.0.1:11304`。
2. **没有登录态** —— `data\sessions\` 为空时需要先点「登录账号」。
3. **浏览器内核没装** —— 执行第 6 步，或跑
   `venv\Scripts\python.exe scripts\verify_bundled_browser.py` 确认。

**Q：报 `Executable doesn't exist at ... run playwright install`？**
内核没装或版本不匹配。执行第 6 步。注意 Playwright 是靠**目录名**（`chromium-<revision>`）
找内核的，所以**升级 playwright 包之后必须重装内核**，否则版本对不上。

**Q：报 `ZoneInfoNotFoundError` / 时区找不到？**
`tzdata` 没装上。执行：

```bat
venv\Scripts\python.exe -m pip install tzdata
```

**Q：高清修复报显存不足（CUDA out of memory）？**
在「高清修复」页调小分块尺寸，或换更轻的模型（如 `realesr-general-x4v3`）。
也可关闭「半精度」以外的加速选项。

**Q：`pip install` 编译 basicsr / gfpgan 失败？**
多为 Python 版本过高（3.12+）或缺 C++ 编译工具。解决办法：
用 **Python 3.10 64 位**重建虚拟环境（第 1、3 步），或安装
[Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)。

**Q：程序为什么这么大？**
主要不是代码 —— 是 PyTorch（约 3 GB）与超分模型（约 1.4 GB）。
换来的是「离线可用 + 不用装环境」。想瘦身可用 CPU 版 torch，代价是推理变慢。

**Q：采集到一半关掉程序，某些账号作品不见了？**
这是设计使然，不是数据丢失：采集采用**替换式记录**，所选账号的旧记录在任务
**开始时**统一清理，之后逐个采集。若中途关闭，排在后面的账号会处于
「旧记录已删、新记录未采」的状态。**下次重新采一遍这些账号即可补齐**，
也可以从 `backups\` 的当日备份恢复。建议一次采集就让它跑完。

**Q：怎么确认自己在跑的是新版本？**
程序内「系统 → 关于」看版本号，与 `README.md` 顶部的版本号比对。

---

## 七、卸载

本程序不写注册表、不装服务，**直接删掉整个程序文件夹即可**。

想保留数据就先备份这几项：

| 要保留的 | 原因 |
|---|---|
| `data\` | 数据库 + 21 个账号登录态（最值钱） |
| `config.json` | 你的配置 |
| `backups\` | 数据库历史备份 |
| `links_log\` `exports\` | 每日归档与导出结果 |

`venv\` `models\` `tools\` `gfpgan\` `runtime\` 可以放心删除 —— 按本文重新准备即可。

---

## 八、免责声明

请仅用于**个人学习**与**已获授权的内容**。GFPGAN 模型按原作者许可仅供个人非商用使用。
下载与处理他人作品可能涉及版权，请自行确认合法性。
