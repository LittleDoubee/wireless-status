# 鼠标信息小窗（WirelessStatus）

一个给**罗技鼠标**用的 Windows 桌面小工具：常驻系统托盘，点一下弹出无边框小卡片，
查看并修改鼠标的关键信息。

技术栈与目录约定沿用同目录下的 WorkBuddy 用量悬浮窗项目：Python 3 + pywebview
（Windows 上走 EdgeChromium / WebView2）、pywin32 做托盘、前端 `index.html` +
`app.js` + `style.css`。

界面分两页：**设备列表**（一屏看到所有配对设备与电量）→ **设备功能区**
（电量、电量历史、DPI、使用时长）。还可以在任务栏上贴一行电量（设备类型图标 + 数值）。

```
wireless-status/
  main.py             主逻辑（双窗口、js_api、命令泵、退出、单实例）
  winui.py            窗口装饰（去任务栏按钮、圆角裁剪、任务栏嵌入、托盘锚点定位、拖动，纯 ctypes）
  devices.py          设备抽象（DeviceProvider / HidppProvider）
  battery.py          电量换算、中值滤波、粒度追踪（1% 计数单位）
  history.py          电量历史（三档聚合、换电池标记、清洗、抽稀）
  usage.py            每日使用时长（500ms 采样、跨午夜结转、休眠校正）
  server.py           本地只读 HTTP（页面 + /api/* 调试接口）
  tray.py             系统托盘（独立线程 + 消息泵）
  autostart.py        开机自启（HKCU Run + --silent，源码/exe 自愈，清理历史别名）
  tipwindow.py        贴片悬停气泡（独立 Win32 顶层窗 + GDI 自绘）
  apppath.py          app_dir() / resource_dir() 与四个 json 的读写
  selftest.py         离线自测（131 项，不需要插鼠标）
  window_state.json   窗口位置（拖动后写入，启动时沿用）
  battery_history.json 电量历史（自动生成）
  usage_stats.json    每日使用时长（自动生成）
  requirements.txt    依赖声明
  start.bat           双击静默启动（无控制台闪现）
  web/
    index.html        两页骨架
    app.js            视图切换、渲染、Canvas 折线图
    style.css         设计变量 + 直角纯白卡片
    pill.html/.js/.css  任务栏电量贴片
  icon/
    app.ico / app.png / mouse.svg / headset.svg / gamepad.svg
    refresh.svg / exit.svg / back.svg
```

**图标缺失不影响使用**：`web/app.js` 里内置了同形状的内联 SVG，`onerror` 时自动
回退，不会出现裂图。托盘图标缺失则没有托盘（那是 pyinstaller 打包问题，见下）。

---

## 一、首次运行

### 1. 装依赖

```bat
python -m pip install --no-deps pywebview
python -m pip install pywin32
python -m pip install hidapi
```

> `--no-deps` 是必须的：不带它 pip 会去装 pyobjc（macOS 专用），在 Windows 上
> 直接报 `PyObjC requires macOS to build` 然后整个安装失败。pywebview 在 Windows
> 上真正需要的只有 pythonnet / clr-loader，一般已经随 pywin32 环境就位；如果
> 启动报 `clr` 相关错误，再单独 `python -m pip install pythonnet`。

`hidapi` 如果装不上（常见于权限受限的 Python 安装目录），两个办法：

```bat
:: 办法一：装到用户目录
python -m pip install --user hidapi

:: 办法二：装到项目里的独立目录，用环境变量指过去
python -m pip install --target .\deps hidapi
set PYTHONPATH=%CD%\deps
```

### 2. 启动

双击 `start.bat`：**静默启动**，不弹窗口、不闪黑框，只在系统托盘出现图标。
整个 bat 只有纯 ASCII（cmd 按 GBK 读文件，中文会乱码），错误走
`logs\start.log` 和消息框，不会因为没控制台就静默失败。

想直接看到界面：

```bat
python main.py --show
```

（不带 `--show` 时是静默启动，和双击 start.bat 一样。）

启动后：

* 系统托盘出现图标（左键单击 = 弹出窗口，右键 = 立即刷新 / 开机自启 / 退出）；
* 窗口固定贴任务栏通知区域正上方，**不在任务栏占按钮**，无边框直角纯白卡片，
  鼠标移出窗口 2 秒自动收起；
* 首次运行会在**程序目录**生成 `config.json`、`window_state.json`、
  `battery_history.json`、`usage_stats.json` 四个文件。

### 3. 自测

```bat
python selftest.py           :: 131 项，全离线，不需要插鼠标
python selftest.py -v        :: 看每一项的名字
```

`selftest.py` **不会**碰真实设备，插不插鼠标、有没有罗技硬件都跑得通。

覆盖范围（131 项）：`app_dir()` 的源码/frozen 两种解析、配置读写往返与未知键
保留、只读降级、DPI 边界与对齐、无设备降级、型号解析失败降级字符串、窗口定位
（物理/逻辑像素）、**托盘图标锚点定位**（`main_rect_near_icon` 的水平居中、
夹进工作区、`anchor=None` 返回 None）、**任务栏嵌入几何**（`pill_rect_in_taskbar`
的高度等于任务栏高、`taskbar_metrics` 结构、`dpi_scale_at` 不抛）、
**托盘 v4 协议**（`NIM_SETVERSION` 常量、`_parse_point` 的 lParam 解析）、
服务器路径穿越、开机自启命令形态、电压→百分比单调性与钳位、
两套曲线、1% 粒度不被取整、历史三档聚合/换电池/脏样本/抽稀/只读降级、使用时长
跨午夜/60 秒分段/休眠不计/**秒级 `HH:MM:SS` 格式（含跨天 `N天 HH:MM:SS` 与负数）**、
**「最后操作时间」已从全链路消失**（后端不下发、前端不渲染、HTML 无节点）、
**拖动接口**（`Api.start_drag`/`finish_drag` 存在、`winui.start_window_drag` 必须用
`PostMessageW` 不能 `SendMessageW` —— 后者会阻塞到拖动结束）、设备列表 0/1/3 台、
非鼠标种类、贴片位置、毛玻璃降级（只对主窗）、图标缺失兜底、
`_MEIPASS` 与 `app_dir()` 不混用、写 DPI 失败时必须带可读原因、
设备不可写时排队命令被立刻拒绝、Win32 显隐函数对无效句柄不抛异常且不得回调
pywebview 窗口方法、带 BOM 的配置能读且读不懂的配置不被覆盖、
品牌名只加一次且不泄露 PID、验收四项都有 DOM 钩子（`TestFieldAlignment`）。

---

## 二、打包成单文件 exe

```bat
python -m pip install pyinstaller

pyinstaller -F -w --name WirelessStatus --icon icon\app.ico ^
  --add-data "web;web" --add-data "icon;icon" main.py
```

> 实操更稳的写法（加 `--noconfirm`，覆盖旧产物不追问；用 `python -m PyInstaller`
> 避免 PATH 里没有 `pyinstaller`）：
>
> ```bat
> python -m PyInstaller -F -w --name WirelessStatus --add-data "web;web" ^
>   --icon "icon\app.ico" --add-data "icon;icon" --noconfirm main.py
> ```
>
> 注意参数名是 **`--icon`**，不是 `--add-icon`（写错会被当成文件路径报错）。

产物：`dist\WirelessStatus.exe`（约 16.5 MB），双击即用，目标机器**不需要装 Python**。
首次运行会在 exe 同目录自动生成四个 json。

### 参数说明

| 参数 | 作用 |
|---|---|
| `-F` | 单文件（onefile），解包到临时目录后运行 |
| `-w` | 无控制台窗口（等价 `--noconsole`）。**注意**：它不会裁掉 pywin32，但会吃掉 stdout/stderr，所以程序里所有异常都落 `error.log` |
| `--icon icon\app.ico` | exe 图标（多尺寸 ico：16/32/48/64/128/256） |
| `--add-data "web;web"` | 把整个 `web\` 目录打进包里。Windows 上分隔符是 `;`（Linux/macOS 是 `:`）。运行时用 `sys._MEIPASS` 定位 |
| `--add-data "icon;icon"` | 同上，图标目录 |

### 两个必须分清的目录

这是打 exe 最容易翻车的地方，代码里由 `apppath.py` 统一处理：

| 内容 | 运行时位置 | 由谁给出 |
|---|---|---|
| `web\*.html/css/js`、`icon\*` | PyInstaller 解包临时目录（`sys._MEIPASS`） | `resource_dir()` / `web_dir()` / `icon_dir()` |
| `config.json`、`window_state.json`、`battery_history.json`、`usage_stats.json` | **exe 同目录**（`os.path.dirname(sys.executable)`） | `app_dir()` → `config_path()` / `state_path()` / `history_path()` / `usage_path()` |

**千万不要**把四个 json 用 `--add-data` 打进去。打进去的后果是：程序启动时
从 `_MEIPASS` 读到的是内置默认值，改完设置写回 exe 同目录，下次启动又读
`_MEIPASS` 里的旧默认值 —— 表现就是「设置永远保存不住」。自测里
`test_resource_and_app_dir_do_not_mix` 专门盯着这条。

### hidapi 的 dll 没带上怎么办

`-F` 打的包有时会漏掉 `hid.cp310-win_amd64.pyd` 依赖的 `hidapi.dll`。症状是
exe 能起、界面能开，但永远显示「未检测到罗技接收器」，日志里写
`No module named 'hid'` 或 `ImportError: DLL load failed`。补法：

```bat
:: 先确认当前环境的 dll 在哪
python -c "import hid, os; print(os.path.dirname(hid.__file__))"

:: 用 --add-binary 显式带上（源路径按上一条的实际输出改）
pyinstaller -F -w --name WirelessStatus ^
  --add-data "web;web" ^
  --add-binary "C:\Python310\Lib\site-packages\hid\hidapi.dll;hid" ^
  main.py
```

更稳妥的做法是用 spec 文件，把 `hiddenimports=["hid"]` 和
`binaries=[("...\\hidapi.dll", "hid")]` 一起写进去。

### 图标与版本号

`icon\` 里已经有全套图标，打包命令里的 `--icon icon\app.ico` 就能带上：

| 文件 | 规格 | 用途 |
|---|---|---|
| `app.ico` | 16/20/24/32/40/48/64/128/256 九档 | exe 图标 + 托盘图标 |
| `app.png` | 256×256 | 备用位图 |
| `mouse.svg` / `headset.svg` / `gamepad.svg` | 24×24，`currentColor` 描边 | 设备列表行图标 |
| `refresh.svg` / `exit.svg` / `back.svg` | 24×24，`currentColor` 描边 | 按钮图标 |

**来源与许可**：全部是本项目**手写的 SVG**（没有引用任何外部素材库/CDN），
`app.ico` 与 `app.png` 由 `tools/make_icon.py` 用 `Pillow` 纯代码绘制（**环形电量表**：灰底环 #7F8083 + 绿色进度弧 #14D448 + 环内绿色闪电，透明背景、无方块底板；16/20/24 小尺寸自动加粗环、拉宽闪电
鼠标 + 右上电量弧）。所以**没有第三方许可约束**，随项目一起分发即可。

要换图标：改 `icon\app.ico` 后重新打包；`tray.py` 会找
`apppath.resource_dir()/icon/app.ico`，找不到就退回系统默认图标（不报错）。

想补版本号元信息，加 `--version-file`：

```bat
pyinstaller -F -w --name WirelessStatus --icon icon\app.ico ^
  --version-file version.txt --add-data "web;web" --add-data "icon;icon" main.py
```

（`version.txt` 是 PyInstaller 的 VSVersionInfo 格式，本轮没做。）

### 打包后自查清单

打完包别急着分发，按这张单子过一遍（每一项都是踩过的）：

| 检查 | 命令 / 做法 | 期望 |
|---|---|---|
| 没有控制台 | 双击 exe | 不弹黑框 |
| 托盘在 | 看右下角托盘区 | 有 `WirelessStatus.exe` 图标；左键单击会弹出窗口 |
| 任务栏没按钮 | 看任务栏 | **不出现**按钮（`WS_EX_TOOLWINDOW` 生效） |
| 启动静默 | 双击后等 10 秒 | 窗口不自己弹出（见「静默启动失效」那节） |
| 外观 | 打开界面看 | 直角、纯白底、无圆角无半透明 |
| 型号读到了 | 列表页 | `罗技 M350 Wireless Mouse` 这种，**不带 PID** |
| 主窗位置 | 点托盘图标 | 窗口底边贴着图标上方约 8px、水平居中于该图标 |
| 主窗可拖 | 按住标题栏空白拖动 | 窗口跟手移动，有边缘吸附；松手后位置存进 `window_state.json` |
| 拖动后重启 | 拖到别处 → 退出 → 重开 | 窗口回到拖动后的位置（不再贴图标） |
| 已运行 | 打开功能区 | `HH:MM:SS` 每秒跳动，无「最后操作时间」；停止操作后再走满 5 秒冻住，不往回跳 |
| 位置不漂 | 开/关贴片、点设备、返回列表 | 窗口左边界与底边全程不动，只有高度跟着内容变 |
| 鼠标移出 | 移开鼠标等 2 秒 | 窗口自动收起 |
| 贴片 | 打开列表页底部的贴片开关 | 任务栏上出现**单行**「设备类型图标 + 90%」，没有底色块/描边/圆角 |
| 贴片长在任务栏里 | 右键任务栏 → 任务栏设置 → 打开「自动隐藏」再退出设置 | 贴片跟着任务栏一起缩下去（不是浮在上面） |
| 贴片开关独占 | 打开后等几分钟 | 一直显示，不会自己消失；关开关 1 秒内消失 |
| 贴片不挡点击 | 点贴片旁边的任务栏图标 | 能正常点到 |
| 悬停显示型号 | 鼠标停在贴片上 | 上方弹出气泡：`罗技 M350 Wireless Mouse` / `USB 接收器 · 电量 90% · 今日用电 x%` |
| 开机静默 | 勾开机自启后重启 | 只在托盘出现图标，**不弹任何窗口/对话框** |
| 贴片降级提示 | 看界面顶部 | 嵌入成功时无横幅；失败时会有一条中文说明 |
| 四个 json 在 exe 同目录 | `dir dist\*.json` | 首次运行后出现 4 个文件 |
| 设置能存住 | 改轮询间隔 → 退出 → 重开 | 值还在（打进去了就会重置） |
| 便携 | 整个目录拷到别处双击 | 设置与历史都跟过去 |
| 无残留 | 退出后看任务管理器 | 没有 `WirelessStatus.exe` 进程 |

### 分发

单文件 exe，无安装器。把 `WirelessStatus.exe` 交给对方即可，首次运行自动在
exe 同目录生成四个 json。要连设置一起给，就把 exe 和四个 json 打包成 zip。

⚠️ **exe 没有签名**，Windows SmartScreen 会提示「未知发布者」，点
「更多信息 → 仍要运行」。要避免提示只能买代码签名证书。

⚠️ **不要放在 `C:\Program Files\` 下**：那里默认不可写，四个 json 存不下来
（程序会降级为内存配置并给提示，但设置退出即丢）。放桌面、文档或任意工具目录。

⚠️ **可能被 Windows Defender 误判**：因为程序会写
`HKCU\...\CurrentVersion\Run` 做开机自启，行为特征像持久化，未签名的新 exe
偶尔会被判成 `Behavior:Win32/Persistence.A!ml` 并隔离（实测遇到过）。
处理办法：在「Windows 安全中心 → 病毒和威胁防护 → 排除项」里把 exe 所在目录
加进排除；或者先编译、签名后再分发。这不是程序在偷偷装东西 ——
只有你在菜单里打开「开机自启」时它才会写那一项，关掉会删掉。

---

### 打包后起不来：`Failed to resolve Python.Runtime.Loader.Initialize`

**这是本项目踩过最大的坑，特意写在这里。** 症状：`python main.py` 一切正常，
打成 exe 后双击闪退，`error.log` 里是

```
RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
...\_MEIxxxxxx\pythonnet\runtime\Python.Runtime.dll
```

**根因**：.NET Framework 4.0 起默认禁止从「远程 / 不受信任位置」加载程序集。
`-F`（onefile）会把所有东西解包到 `%TEMP%\_MEIxxxxxx`，CLR 认为那是远程来源，
于是 `pyclr_get_function` 对**任何**程序集（连系统自带的 `mscorlib` 都算）都
返回 NULL。pywebview 在 Windows 上只走 winforms 后端，那条路强制 `import clr`，
所以 pythonnet 一挂，整个窗口就起不来。

**修法已经写在代码里**：`main.py` 顶部的 `prepare_dotnet_runtime()`，函数体就一行

```python
os.environ.setdefault("COMPLUS_LoadFromRemoteSources", "1")
```

关键是它**必须在 `import webview` 之前**执行（`WirelessStatusApp.run()` 一进来就
调它）。等价于在 exe 旁边放一个 `WirelessStatus.exe.config`：

```xml
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <runtime>
    <loadFromRemoteSources enabled="true"/>
  </runtime>
</configuration>
```

**下面这些做法都试过、都没用**，别再重复：

| 尝试 | 结果 |
|---|---|
| `--collect-all pythonnet --collect-all clr_loader` | dll 确实带全了（99 个文件，MD5 与源一致），但仍然报同一个错 |
| `os.add_dll_directory(_MEIPASS)` | 无效 |
| 把 `Python.Runtime.dll` 复制到 exe 同目录 | 无效 |
| 改成 onedir（不是 onefile） | 同样失败 ⇒ 不是路径问题 |
| `exe.config` 加 `supportedRuntime v4.0` + `useLegacyV2RuntimeActivationPolicy` | 无效 |
| 环境变量 `COMPLUS_Version=v4.0.30319` | 无效 |
| 换 `PYTHONNET_RUNTIME=coreclr`（.NET 8） | 能 `CLR OK net 8.0.3`，但 .NET 8 的 WinForms 删了 `ContextMenu`，WebView2 控件加载抛 `TypeLoadException`，走不通 |
| 降级 pythonnet 2.5.2 | 装不上（`Failed to build installable wheels`） |

### 打包后的窗口跑到屏幕外（150% 缩放）

**症状**：exe 双击后进程在、托盘图标在，但看不到窗口；任务栏点它也没反应。

**根因**：进程调 `SetProcessDpiAwarenessContext` 之后，
`SystemParametersInfoW(SPI_GETWORKAREA)` / `GetSystemMetrics` 返回的是**物理像素**
（1920×1080 的屏在 150% 下报 2560×1600）；而 pywebview 的
`create_window(x=, y=, width=, height=)` 走 WinForms 的 Location/Size，用的是
**逻辑像素**。不换算就会把窗口放到物理 3216 这种屏幕外的位置。

**修法**：`main.py` 里的 `dpi_scale()` 用 `GetDeviceCaps(LOGPIXELSX) / 96.0`
拿到比例，`screen_work_area()` 统一除回逻辑像素再返回给调用方。凡是「拿去和
pywebview 坐标比」的值都必须走这套逻辑像素。

排查手法（Windows 上很好用）：

```powershell
# 看窗口真实落点；如果 rect 的 x 比屏宽还大，就是这个问题
Add-Type -AssemblyName System.Windows.Forms
Get-Process WirelessStatus | Where-Object { $_.MainWindowHandle -ne 0 } |
  ForEach-Object { $_.MainWindowTitle; $_.MainWindowHandle }
```

确认是它的话，把 `window_state.json` 删掉（或把 `x`/`y` 改成 `null`）能让程序
按「首次启动」重新摆位置。

### 界面全白 / 样式全丢

检查 `web/style.css` 顶部是不是被误加了 Python 的文件头（`# -*- coding: utf-8 -*-`）。
CSS 里 `#` 起头是 ID 选择器，那条注释会被当成选择器，把紧跟其后的 `:root { ... }`
整块吃掉 —— 表现是**文字能显示、但颜色/圆角/按钮全退回浏览器默认**。

### 托盘左键只能开、不能关（或点一下就卡住）

> 现版本左键语义已改为「只显示」，不再 toggle，所以这条症状不会再出现；
> 下面记的是当时的根因与修法，留着防回归。

**根因：从非 UI 线程调用 pywebview 的窗口方法会永久阻塞。**

`self.window.move()/resize()/show()/hide()` 内部都要等 UI 线程；而命令泵线程
（托盘回调、`set_height` 都往它投递）等它的时候，UI 线程可能正卡在别处，
于是**调用方再也回不来**。实测症状很具迷惑性：`move()` 里的
`SWP_SHOWWINDOW` 已经把窗口带出来了，但下一行 `resize()` 卡死，
`self._visible = True` 之后的状态再也没更新 —— 表现成「点一下能开，再点没反应」。

**修法：显隐与几何全部走纯 Win32**（`winui.show_window()` /
`winui.hide_window()` / `winui.move_resize()` / `winui.set_topmost()`），
命令泵线程**一次都不碰** pywebview 的窗口对象。`main.py` 里
`show_window()` / `hide_window()` / `_reposition()` / `set_height()` 都已经改成这条路。

自测里加了两道防回归（`test_win32_visibility_helpers_tolerate_bad_handle` /
`test_win32_helpers_do_not_touch_pywebview`），后者直接扫源码文本，
防止以后有人又写回 `window.show()`。

### 双击后窗口自己跳出来了 / 静默启动失效

`pywebview` 的 `window.move()` 内部是
`SetWindowPos(..., SWP_NOSIZE | SWP_NOZORDER | SWP_SHOWWINDOW)` —— 那个
`SWP_SHOWWINDOW` 会**把隐藏的窗口强制显示**。所以只要程序在隐藏状态下挪过
一次窗口（`_reposition()`、前端的 `set_height()` 都会挪），窗口就冒出来了。

v2 起几何已经改走纯 Win32（不带 `SWP_SHOWWINDOW`），这个问题从根上没了；
`_restore_visibility()` 仍保留作兜底。自测跑不出来这条（要真窗口），只能实机验证：

```powershell
cmd /c start.bat
Start-Sleep 10
# 期望 False；如果是 True，说明某处 move 后没补 _restore_visibility()
python -c "import ctypes;u=ctypes.windll.user32;h=u.FindWindowW(None,'Wireless Status');print(bool(u.IsWindowVisible(h)))"
```

### 窗口高度不跟着内容走 / 一直是大片空白

两个坑叠在一起：

1. **`.scroll` 不能是 `flex:1`**。内容是 flex 子项时它会**撑满剩余空间**，
   `scrollHeight` 就恒等于「窗口高 - 固定块」。拿它做自适应会形成正反馈：
   窗口变高 → 量到更高 → 再变高，一路顶到 `MAX_HEIGHT`。
   正确做法是 `flex:0 1 auto`（按内容收缩）+ `.footbar { margin-top:auto }`
   （把底栏推到底），然后**累加滚动区子元素高度**来量内容。
2. **`min_size` 必须跟着 `MIN_HEIGHT` 降**。pywebview 的 `resize()` 受系统
   最小尺寸约束，`min_size` 写死 `(340, 420)` 的话，前端报 168 会被弹回 420
   —— 表现是「列表页只有一行设备，窗口却停在 630 下不去」。
3. **`fitHeight` 的滞回别开太大**。原来写了 `if (Math.abs(want - 已报高度) < 24) return`，
   于是 168 和 190 之间那点差永远不生效，设备下方留一条空白。现改成 2px，
   且 `want = Math.ceil(content + fixed + 2)`。

### 贴片四角有深灰边 / 贴片背后像垫了块黑方块

**根因：给贴片窗也套了 DWM 毛玻璃。** 亚克力/模糊是按窗口**包围盒**画的，
不认 `SetWindowRgn` 裁出来的形状，所以四角外侧会露出一圈 `#3D3D3D`。

**修法**：`_apply_window_chrome()` 里 `apply_blur()` 只对 `name == "main"` 调用，
贴片只保留 `hide_from_taskbar` + `clip_round_rect` + `set_topmost`。

顺手排掉但**不是**原因的几项（别再试）：

* 关 WebView2 的 DirectComposition（`--disable-direct-composition`）—— 无效；
* 改页面底色（临时 `background:#ff00ff`）—— 四角仍是深灰，说明不来自 HTML；
* 陈旧像素（`InvalidateRect` 任务栏）—— 无效；
* region 没生效 —— 换成 `CreateEllipticRgn` 大圆后中心也变深，证明 region 是生效的。

**判定手法**：采样窗口内四角颜色，再把窗口隐藏后采同一坐标。两者一致 =
裁剪生效、深色来自窗口装饰；不一致 = 裁剪或装饰还有问题。

### 贴片是一块「淡淡的方印子」/ 底色和任务栏对不上

扁平样式下贴片没有底色块，窗口底色**就是**任务栏采样色，所以底色差几级
就会显出一个方块轮廓。两个独立原因，别混：

**原因一：任务栏横向有渐变（主因）。** 实测通知区附近：

```
x=1700 → (209, 221, 221)
x=1850 → (216, 229, 228)
x=1900 → (224, 236, 233)
x=2000 → (230, 234, 234)
```

90 像素宽的贴片横跨这段就是 8~20 级差。**填一个纯色，无论取哪个点，总有
一条边对不上。**

修法：`winui.taskbar_background_profile()` 沿贴片自己的横向区间采 7 个点，
前端画 `linear-gradient(to right, ...)`。纵向必须取贴片**上方/下方那两条
空白**（贴片只占任务栏高度的一部分），不能在垂直中点采样 —— 那里是图标
和「90%」的笔画。

**原因二：显示器色彩管理的残余色差（无法完全消除）。**
同一个 8bit 值，任务栏和我们的窗口在屏幕上落到不同像素：

```
任务栏自己的像素   (222, 234, 234)
我们写 #dce8e8     (220, 232, 232)
屏幕量到的窗口像素 (216, 234, 234)   ← R−4 G+2
```

纯 GDI 的 tkinter 窗口填同一个值，屏幕像素**同样**是 (216,234,234) —— 所以
既不是采样错，也不是 WebView2 的锅。

**试过但无效**（别再试）：

* `--force-color-profile=srgb`（`AdditionalBrowserArguments`）—— 无效；
* 按实测差值做闭环补偿（读回窗口像素、把差值补上去）—— **不收敛**：
  实测写 (225,233,233) 屏幕回 (228,231,233)、目标 (222,234,234)，每拍都在
  目标两侧跳；因为这个变换既不是加性偏移、也不完全单调（实测 R 通道
  228→209 变化 19 级、G 通道 232→237 只变 5 级）。

**结论**：渐变采样点已是可达的最优解，残差 1~2 级，正常视距看不出边界。
不要为了实现"绝对一致"再去叠补偿 —— 实测只会更花。

### 悬停贴片没有任何提示

**根因：网页 tooltip 被窗口裁剪吃掉了。** 贴片窗只有 26 逻辑像素高，
还被 `SetWindowRgn` 裁成内容形状 —— **WebView2 画不出窗口之外的东西**，
`.chart-tip` 一弹就被裁，屏幕上什么都没有。

**修法**：改成 `tipwindow.py` 的独立 Win32 顶层窗（自己的线程 + 消息泵，
`WS_POPUP | WS_EX_NOACTIVATE | WS_EX_TOPMOST`，GDI 自绘）。前端只负责把
「现在悬停的是哪台」通过 `api.set_pill_hover(key, True)` 报给后端。

三个容易再踩的坑：

* `CreateWindowEx` 的 style **必须显式给 `WS_POPUP`**，传 0 时 pywin32 会
  退回"带标题栏的默认窗口"，弹出的是一条带 caption 的白条；
* `win32gui` **没有** `CreateFont` —— 走 ctypes 调 `gdi32.CreateFontW`，
  并声明完整 14 个 `argtypes`（不声明时 ctypes 按调用现场猜参数个数，
  多传一个就 `TypeError: this function takes 12 arguments (14 given)`）；
* 跨线程只能 `PostMessage`（`WM_TIP_SHOW` / `WM_TIP_HIDE`），不能直接
  `SetWindowPos`。

### 开机后自己弹出一个「已经在运行了」的对话框

三个原因叠在一起，必须一起修：

1. **注册表有两条**。同一个程序在 `Run` 下被注册两条时，开机 explorer 会把
   两个进程都拉起来。修法：`autostart.prune_legacy()` 启动时遍历 `Run`，
   凡是**命令行指向本程序**、值名又不是规范名的项一律删掉
   （判断靠 `_looks_like_own_command`，别人的项不动）。
   ——按命令行内容判断，而不是"写死某个旧名字"：后者每改一次名就得回来补
   一次黑名单，而且旧名字会一直留在代码里。
2. **第二个实例会唤窗**。老代码拿不到单实例锁就 `ShowWindow` 把已有窗口
   顶出来。修法：只有显式带 `--show` 才唤窗，其余（含开机 `--silent`）
   静默退出。
3. **唤窗失败还弹框**。老代码兜底 `MessageBoxW("已经在运行了")`。修法：
   整条二次启动路径里不再有任何 `MessageBoxW`（`selftest` 有断言盯着）。

另外启动失败也只落盘、不弹窗（`_report_fatal(silent=...)`）—— 开机时弹一个
模态框会挡住后面所有自启项，用户还常常不在机器前。

唤窗本身用的是**命名事件**（`WirelessStatus.ShowRequest.v3`），不是
`PostMessage`：主窗的 WndProc 属于 pywebview（WinForms），外部挂不上自己
的消息处理，发自定义消息进去会被默认分支吃掉。


---

## 三、支持的设备

### 支持

* **通过 Nano 接收器（USB）接入的 HID++ 2.0 鼠标**。已实测：**M350 Wireless Mouse**
  （PID `B021`，接收器 PID `C52F`）。
* 界面显示格式是「品牌 + 产品名」，即 **`罗技 M350 Wireless Mouse`**，不带 PID
  （PID 只在日志和排查里出现）。产品名读不到时降级为 `罗技 未知型号`，不会留空。
* 判定方式：`hid.enumerate(0x046D, 0)` 里找 `usage_page = 0xFF00`、`usage = 2` 的
  长报文接口（report ID `0x11`），然后在 device index 1–6 上依次 ping，谁回谁就是
  接收器上配对的那只鼠标。**型号不硬编码**，运行时读 HID++ `0x0005`（DeviceName）
  与 `0x0003`（DeviceInformation）拿真实产品名与 PID。
* DPI 能力（`0x2201`，只有扩展版时走 `0x2202`）与电量能力（`0x1000` / `0x1004`）
  都是**按设备实际枚举到的特性**判断，不按型号猜。

### 已知不支持 / 有限支持

* **蓝牙直连的鼠标**、**USB 线直连**：本工具走的是接收器通道，这两条路径没有对接。
  以后要加，需要改 `mouse_source._find_receiver()` 的枚举条件（蓝牙走另一组
  usage page，直连走设备自身的 HID++ 集合）。
* **只有短报文接口（report ID `0x10`）的接收器**：会报
  「找到罗技 HID++ 接口，但没有长报文集合」。`0x2201` 等特性读不了。
* **没有 `0x2201` 也没有 `0x2202` 的鼠标**：DPI 区整体禁用，界面标注「推断值」，
  但滑块拖不动、按钮是灰的（这是正确行为，不是 bug）。
* **只有 `0x1001`（电压）而没有 `0x1000` / `0x1004` 的鼠标**：电量显示「不支持」。
  电压换算百分比各机型不一致，硬换算出来的数字是骗人的。
* **带 `0x1004`（UnifiedBattery）的机型**：该特性的字段布局各家不一致，代码里
  采取保守策略 —— 解出的值在 0–100 之间才当百分比用，否则显示「不支持」。

### 多设备

接收器上挂了多只设备时，**全部列出来**，不是让用户选一个（`device_index`
只用来决定「优先 probe 哪一台」，`0` = 第一个应答的）。

但要注意一个性能取舍：HID++ 要一台台 ping + 读特性，6 个索引全 probe 一轮要
好几秒。所以轮询里**只 probe 当前那一台**，其余设备只保留「索引 + 上次见过的
信息」，点进去才补全。列表里它们会显示 `罗技 未知型号`，连接那行写
`USB 接收器 · 设备索引 3` —— 这是正常的，不是读失败。

想换主设备：点那一行进功能区即可，不需要改配置重启（`last_selected_device`
会记住）。

---

## 四、读不到 DPI / 电量怎么排查

按顺序看这三处。

### 1. 看界面日志

界面上的日志入口本轮已经去掉了（需求要求删除），日志现在走两条路：

* **源码运行**：直接看控制台输出（`python main.py --show`）。
* **exe / pythonw**：看 `wireless-status\logs\start.log`（start.bat 写的启动记录），
  运行时异常看同目录的 `error.log`。

正常启动应该有这几行：

```
09:54:35  接收器已打开：PID C52F（Nano 接收器）USB Receiver
09:54:36  接收器上应答的设备索引：1
09:54:36  电量来源：0x1000（battery）
09:54:37  DPI 能力（传感器 0）：400–4000 步进 100，原始 0190 E064 0FA0
09:54:37  设备就绪：M350 Wireless Mouse | PID B021 | unit 4080CFB3 | transport 0x06 | 特性 battery,dpi,info,name
```

对应关系：

* 没有第一行 → 接收器没插好 / 没有长报文接口 / hidapi 没装；
* 有第一行但没有第二行 → 鼠标关机、深度休眠，或配对丢失（动一下鼠标再看）；
* 有第二行但没有第三行（`电量来源`）→ **电量读不到**，日志里会有
  `设备没有可读的电量特性（0x1000 / 0x1001 / 0x1004 均不存在）`；
* 有第三行但没有第四行（`DPI 能力`）→ **DPI 读不到**。日志会写
  `设备没有 0x2201/0x2202 特性，DPI 不可调（按推断值显示）`，或者
  `读 DPI 能力失败：...`；
* 有 `电量来源：0x1001` 说明走的是**电压换算**，界面会标注「估算」并附 mV ——
  这是设备能力的限制，不是 bug。

同样的日志在控制台也能看到（`python main.py --show` 时）。

### 2. 用 `mouse_source.py` 单独跑

```bat
python mouse_source.py
```

它不建窗口，直接采一次并把完整 JSON 打出来。适合在别人机器上确认
「到底是设备问题还是窗口问题」。

### 3. 抓 HID++ 原始报文

**推荐工具**：

* [Solaar](https://github.com/pwr-Solaar/Solaar)（Linux）或
  [Logi Options+](https://www.logitech.com/software/logi-options-plus.html) 的日志；
* Windows 上用 **Microsoft Message Analyzer** 或开源的 **USBPcap + Wireshark**
  （装 USBPcap 时勾选你要抓的 root hub，过滤器写 `usb.idVendor == 0x046d`）；
* 最轻的：项目里的 `mouse_source.py` docstring 已经写明帧格式，可以照抄
  一个 20 行的探针脚本自己发。发帧规则：

  ```
  长报文：[0x11, devIndex, featIndex, (func<<4)|swid, params...] 补零到 20 字节
  短报文：[0x10, devIndex, featIndex, ...]                       共 7 字节
  拿特性索引：[0x11, devIndex, 0x00, 0x00, FEATURE_ID_MSB, FEATURE_ID_LSB, ...]
  ```

**踩过的坑**（代码里都处理了，自己写探针时注意）：

* 特性 ID 在请求里是 **MSB 在前**，写反了特征索引永远是 `0x00`；
* `0x0000` 的 ping 帧，byte3 直接是 `0x1F`，不是 `(func << 4)`；
* 回包必须按 `r[1] == devIndex` 过滤，否则会读到上一轮的迟到帧（实测串帧）；
* `getSensorDpiList` 的 16 位列表从 **`r[5]`** 开始（前面是 `sensorIdx`），
  从 `r[4]` 起会整体错位一字节，解出「1–40960 步进 15345」这种离谱值；
* **写 DPI 时设备不夹取也不校验** —— 把 1 或 65535 写进去它会原样回显，
  所以上下限只能来自 `getSensorDpiList`，不能靠写极值探测。

### 4. 接口被独占

`Logi Options+` / `G HUB` 会一直占着接收器的长报文接口。症状是接收器能打开
但所有请求都超时。关掉它们再试；两者可以共存，但要先让本工具连上再开它们。

---

## 五、配置文件位置与便携拷贝

### 位置

`config.json`、`window_state.json`、`battery_history.json`、`usage_stats.json`
**四个都写在程序所在目录**：

| 运行方式 | 目录 |
|---|---|
| 源码运行 | `wireless-status\`（脚本所在目录） |
| exe 运行 | `WirelessStatus.exe` 所在目录 |

**不写 `%APPDATA%`**，也不写注册表以外的任何位置。这就是「绿色便携」：
整个目录拷走，设置和统计跟着走。

| 文件 | 内容 | 能不能删 |
|---|---|---|
| `config.json` | 所有设置 | 删了下次启动按默认重建 |
| `window_state.json` | 上次窗口位置（拖动后写入，下次启动沿用） | 删了下次启动重新贴托盘图标 |
| `battery_history.json` | 电量历史（32 天） | 删了历史曲线清零 |
| `usage_stats.json` | 每日使用时长（30 天） | 删了「已运行」清零 |

### 可写性

程序启动时会探一次目标目录能不能写（建个临时文件再删）。写不了（比如放在
`C:\Program Files\` 下）时降级为**纯内存配置**：程序照常跑，设置改了也生效，
但**退出即丢**，界面上会挂一条黄色提示「配置目录不可写」。想保存设置就把
程序挪到桌面或文档目录。

四个 json 是**各自独立**降级的：`config.json` 写失败不影响历史落盘，反之亦然。

### 便携拷贝

```
copy WirelessStatus.exe     D:\tools\mouse\
copy config.json          D:\tools\mouse\
copy window_state.json    D:\tools\mouse\
copy battery_history.json D:\tools\mouse\
copy usage_stats.json     D:\tools\mouse\
```

双击 `D:\tools\mouse\WirelessStatus.exe`，DPI 范围、轮询间隔、窗口位置、贴片开关、
开机自启开关、电量历史、使用时长全都还在，不用重新配。

⚠️ **开机自启例外**：它写在注册表
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 里，存的是**绝对路径**。
把 exe 挪走后，注册表里的路径就失效了。程序下次启动会自动检测到并改回当前
路径（`autostart.heal()`，启动时执行），但**在你挪走之后、启动之前**那次开机
会启动失败。所以搬完家记得先手动启动一次。

### 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `version` | `2` | 配置结构版本（本轮从 1 升到 2，旧的键全部保留） |
| `poll_interval_sec` | `5` | 窗口可见时的轮询间隔（秒） |
| `poll_interval_hidden` | `30.0` | 窗口隐藏时的轮询间隔（秒），降频降 CPU |
| `device_index` | `0` | 接收器上有多只设备时**优先**看哪只；`0` = 第一个应答的 |
| `autostart` | `false` | 记录用户意图（真实状态以注册表为准） |
| `always_on_top` | `false` | 兼容旧版本保留；本轮窗口恒置顶 |
| `low_battery_threshold` | `5` | 低于这个百分比，电量红色高亮 |
| `window.width/height` | `392/588` | 默认窗口尺寸（之后由前端按内容自适应） |
| `dpi_range` | `200–12000` 步进 50 | 设备能力读不到时的推断值（界面上会标注「推断值」） |
| `battery_pill_enabled` | `false` | 任务栏电量贴片开关（列表页底部那个） |
| `battery_curve` | `"alkaline"` | 电压换算曲线：`alkaline` 碱性 / `nimh` 镍氢 |
| `battery_levels` | `{good:50, low:20, critical:5}` | 电量颜色分档（≥good 绿、≥low 黄、≥critical 橙、其余红） |
| `last_selected_device` | `null` | 上次进过功能区的设备 key（只用来高亮，不自动跳页） |
| `theme` | `"auto"` | **已废弃**：界面固定浅色一套，这个键只为兼容旧配置保留，改它没有效果 |

**未知键会被原样保留**：你自己加的键，程序写配置时不会抹掉（自测里有专门一条
`test_unknown_key_preserved_on_rewrite` 盯着这个行为）。

### 配置被重置成默认值 / 手改的配置不生效

两个原因，都在 `apppath.py` 里修掉了：

1. **带 BOM 的 UTF-8 读不进来**。Windows 记事本、PowerShell 5.1 的
   `Set-Content -Encoding UTF8`、以及不少编辑器另存的「UTF-8」都是**带 BOM** 的
   （文件头 `EF BB BF`）。早期实现用 `encoding="utf-8"` 读，抛
   `JSONDecodeError: Unexpected UTF-8 BOM`，再被当成「没有配置文件」→
   **当场用默认值把用户文件覆盖掉**。现在读配置统一用 `utf-8-sig`。
   自测：`test_reads_config_with_bom`。
2. **「读不懂」被当成「不存在」**。以前的 `_load_or_create()` 对两种情况都走
   「写默认值」分支。现在只有文件真的不存在才生成默认值；文件存在但解析失败时
   **保持内存默认值、不写盘**，并往 `ui_error.log` 记一行
   `config: 配置文件无法解析…本次使用内置默认值且不会覆盖原文件`。
   自测：`test_unreadable_config_is_not_overwritten`。

自己检查当前文件有没有 BOM：

```powershell
[System.IO.File]::ReadAllBytes("config.json")[0..2] -join ','   # 239,187,191 = 有 BOM
```

有 BOM 不用改，程序现在能读；只是想清掉的话，用 `python -c` 或 VS Code
「以 UTF-8（无 BOM）保存」重存即可。

---

## 六、界面与操作

### 设备列表页

* 顶部标题 `设备`，右上角**从左到右**是**刷新**（转圈 600ms）和**退出**。
* 每个配对设备一行：图标 / `罗技 M350 Wireless Mouse` / 连接方式 + 状态色点 /
  电量百分比 + 颜色。**型号不显示 PID**，格式统一是「品牌 + 产品名」。
* 鼠标悬停在设备行上底色加深（`#eef1f4`），按下再深一档（`#e3e7eb`）。
* 点一行进功能区；`last_selected_device` 会让上次那台保持高亮，但**不自动跳页**
  —— 这是刻意的，启动时先给全局视图。
* 退出要在页面内二次确认（「确认退出？」+ 取消/确认，**5 秒不动自动收起**）。
  不用系统对话框，因为那个会阻塞 webview 线程。
* 底部一行「任务栏电量贴片」开关，切了立刻生效并写进配置。
* 没设备时显示「未检测到设备」+ 排查说明，刷新按钮**保持可用**。
* 窗口高度跟着内容走：一台设备就是个紧凑小卡片，设备多了往上长，超过
  `MAX_HEIGHT`（760）才内部滚动。**设备下方不留空白**（`fitHeight` 的滞回从
  24px 收到 2px，且 `.scroll` 不再 `flex:1`，否则量高会正反馈越量越高）。

### 设备功能区

* 左上角 `←` 回列表（只切视图，不销毁不重载，所以不用等页面重新加载）。
* 设备头：图标 / `罗技 M350 Wireless Mouse` / 连接方式 / 状态胶囊。
* **验收要求的关键四项在这一页**：型号、连接状态（色点 + 状态胶囊）、
  电量（环形百分比）、DPI（滑块 + 数值）。
* 另有：已运行（当日使用时长，`HH:MM:SS`，每秒跳动）、电量历史（24 小时 / 7 日 / 30 日
  三个 Tab）、任务栏电量贴片开关（列表页底部）。
* **待机时间已移除**：`GetLastInputInfo` 给的是**全系统**空闲时长，不是这只鼠标的，
  放在「设备详情」里容易被误读成设备读数，所以整块删掉（`devices.py` 也不再透传
  `idle` 字段，自测里有 `test_idle_not_leaked_to_device` 守着）。
* **开机自动启动开关也从这一页移除了**，只剩托盘右键菜单里那一项
  （系统级开关放在设备详情里位置不合适）。
* **已运行**的口径写在卡片底部：`光标移动或按键（500ms 采样）`。停止动作后再
  平滑走满 **5 秒**才停表（`IDLE_TIMEOUT_SEC = 5`），跨 00:00 自动切给两天，
  休眠唤醒按时间差校正不计入。
  格式是 `HH:MM:SS`，超过 24 小时显示 `N天 HH:MM:SS`。前端每秒本地 +1 秒
  （只在「这次轮询值比上次大」时才走，否则冻住 —— 避免凭空涨时间再跳回），
  不重排整页，所以不闪。
  **三处口径必须一致**：收段末端算到 `min(now, last_active + IDLE)`，
  `flush_segments()` 与 `today()` 都用同一个 `_live_end()`。不一致就会出现
  「数字先冻住、过几秒突然 +5、偶尔还往回跳」—— 那是 `today()` 把段还没落账
  的将来时间也算进去了，段一收就被截断回来。
* **「最后操作时间」已删除**：这个值只能反映「什么时候有键鼠动作」，对「这只
  鼠标用多久」没有增量信息，反而让卡片更挤。后端 `usage.today()` 不再下发
  `last_active` / `last_active_text`，前端也删掉了对应节点与渲染；自测里有
  `test_last_active_gone_everywhere` 守着，防止哪天后端又把它加回来。
* **DPI**：滑块 + 大号数值 + 「应用 DPI」+ 「恢复默认」。范围读自设备
  （`0x2201` 的 DPI 列表），读不到时用 200–12000 并标「推断值」。写入后**回读
  一次**确认，失败会明确说是「设备不支持 / 权限不足 / 通信超时」。
  设备并不夹取越界值（写 21000 会被原样回显），所以上下限只认读到的能力。
  「应用 DPI」是**同步等结果**的：点下去会一直等到设备回读确认（最多 4 秒），
  成功显示「已写入并回读确认：1600 DPI」，失败/超时/设备离线都会弹具体原因，
  不会出现「点了没反应」。
* **电量历史**：Canvas 手画折线，不引图表库；hover 显示时间/电量/电压；
  向上跳超过 20% 视为换电池，画虚线竖线；摘要显示「今日消耗 / 日均」。

### 任务栏电量贴片

列表页底部开关打开后，任务栏右端、**紧贴「显示隐藏的图标」箭头左侧**出现一枚
小胶囊（在线设备最多 4 枚，超出显示 `+N`），颜色分档跟列表页一致。观感参考
雷柏 Rapoo 那种「占据任务栏空间」的贴片：胶囊浅灰底
（`#f7f8fa`）+ 细边框，量出来多宽窗口就只有多宽。

**这枚胶囊是真的长在任务栏里的**（v3 起）：

* 用 `SetParent` 把贴片 hwnd 挂成 `FindWindow("Shell_TrayWnd")` 的子窗口，
  样式改成 `WS_CHILD`（去掉 `WS_POPUP`、加 `WS_CLIPCHILDREN`），横向位置按
  通知区域左边界算，**窗口高度就是胶囊高度、在任务栏内垂直居中**。
  （早期版本让窗口占满整条任务栏、靠 `SetWindowRgn` 裁出胶囊，结果胶囊外侧会
  露出窗口底色 —— 在浅灰任务栏上就是一圈白边。现在窗口和胶囊一样大。）
* 因此它会跟着任务栏一起**绘制、遮挡、自动隐藏**（任务栏设成自动隐藏时一起缩）、
  跟着任务栏的 DPI 缩放走，不在 Alt+Tab 里出现，也不产生任务栏按钮
  （`WS_EX_TOOLWINDOW` + `WS_EX_NOACTIVATE`，点它不抢焦点）。
* **底色跟任务栏走，不写死**：Win11 任务栏是亚克力，颜色随壁纸浮动，写死
  `#f7f8fa` 会变成「贴上去的白补丁」。运行时 `BitBlt` 抓任务栏、跳过半透明
  与图标笔画、直方图取众数，据此推导胶囊底色（提亮 2~4 级）与边框。
  **只在贴片左侧采样** —— 右侧 80 px 落进 `TrayNotifyWnd` 的中性灰底，
  会把众数拉偏 20 级。
* **先设几何再裁形状**：`SetWindowRgn` 的区域是窗口坐标，窗口被 `SetWindowPos`
  改大改小时区域不跟着缩放，先裁会「右边缺一块」。所以 `_apply_pill_host()`
  的顺序是 `embed_in_taskbar` → `clip_round_rect`。
* **字号按任务栏高度分档**：`pill.js` 每 30 秒问一次 `api.taskbar_metrics()`，
  `logical_height` ≥56 用 `bar-xl`、≥44 用 `bar-lg`、否则 `bar-sm`（11/12/13px）。
* **深色任务栏自动换配色**：`taskbar_metrics().dark` 读的是注册表
  `SystemUsesLightTheme`（任务栏主题，比 `AppsUseLightTheme` 准），深色时加
  `bar-dark` 类，底面 `#2b2b2b`、字 `#f2f2f2`，保证两种任务栏下都能读。
* **嵌不进去时有降级路径，且会说出来**：Win11 上 `SetParent(Shell_TrayWnd)` 在
  个别配置下会被系统拒绝或被裁剪掉。此时 `_apply_pill_host()` 会退化成
  「紧贴任务栏内的无边框置顶窗口」（几何与嵌入态一致，观感几乎无差别），
  并把中文原因写进 `snapshot.pill_error`，界面顶部会显示一条横幅说明。
  **不会静默退回旧行为**。
* **开关独占控制**：打开后一直显示，**没有任何定时/超时隐藏**；唯一关闭入口
  是列表页那个开关（或退出程序）。关掉会 `DestroyWindow` 并清掉 HWND 缓存，
  1 秒内从任务栏消失；再打开会建一个全新的窗口重新 `SetParent`。
  状态写在 `config.json` 的 `battery_pill_enabled`，启动按配置恢复。

* **窗口宽度跟随内容**：`pill.js` 累加子元素实际宽度（不是读容器的
  `scrollWidth` —— 容器是 `width:100%`，那样永远收不窄）→ `api.set_pill_width()`
  → `SetWindowPos` + 重新裁圆角。窗口不比内容宽，是为了**不挡住任务栏下层的点击**。
* **形状靠 `SetWindowRgn` 裁**，不是靠 CSS 圆角也不是靠透明：pywebview 在
  WinForms + WebView2 上的 `transparent=True` 并不做逐像素透明，实测就是一整块
  不透明矩形（底色是被 `background_color` 顶出来的黑）。所以贴片窗是
  **不透明窗 + `CreateRoundRectRgn` 裁出胶囊形状**，区域外不可见也不吃鼠标。
* **贴片窗不套毛玻璃**：DWM 的亚克力效果按窗口**包围盒**画，不认 region 裁出来的
  形状 —— 套上去胶囊四角外侧会冒出一圈深灰（`#3D3D3D`），看着像背后垫了块黑方块。
  所以 `_apply_window_chrome` 里只给主窗套模糊。
* 悬停显示「设备名 · 今日用电量」的 tooltip（往上弹，避开任务栏）。
* 点任意胶囊打开主窗。
* **不占任务栏按钮**（`WS_EX_TOOLWINDOW`），不可拖动。
* 分辨率/缩放变化后自动重算位置（30 秒复核一次任务栏度量）。

### 主窗

* **位置跟着托盘图标走**：点托盘图标后，窗口**底边贴在图标正上方 8 逻辑像素**，
  水平以图标中心居中。锚点来自 `Shell_NotifyIcon` 的 `NOTIFYICON_VERSION_4`
  回调（`NIN_SELECT` 的 lParam 低 16 位是图标 x、高 16 位是 y），拿不到时退回
  通知区域中心。
* **多显示器 / 多 DPI / 任务栏换边都能适配**：`MonitorFromPoint` 取锚点所在显示器，
  `GetDpiForMonitor` 取那块屏的缩放，用 `GetMonitorInfo` 的工作区做 clamp；
  上方放不下就翻到图标下方；任务栏在左/右/上时按「工作区与显示器矩形差集」
  反推任务栏方向。每次显示都重算，所以换分辨率、改缩放、换主屏都能自动跟上。
* **拖动过就记住位置**：拖完写 `window_state.json`，下次启动直接沿用（不再贴图标），
  直到下次点托盘图标才重新按图标居中覆盖。
* **不在任务栏出现图标**：`GWL_EXSTYLE` 加 `WS_EX_TOOLWINDOW`、去掉
  `WS_EX_APPWINDOW`，owner 设成托盘的隐藏窗口，再拿 `ITaskbarList::DeleteTab`
  兜底。
* **外观**：无边框、**四个角统一直角**、背景纯白、浅灰细边框。早期版本用
  `border-radius: 18px` + 透明窗做圆角，结果是「第一次弹出是圆的、重绘后变方」，
  且 `transparent=True` 在 WebView2 上透不出桌面，所以现在统一直角实色。
* **整个界面可以拖**：标题栏、卡片空白处按住就能拖动（`cursor: grab` →
  拖动中 `grabbing`）。实现是 `ReleaseCapture()` + `PostMessage(hwnd,
  WM_NCLBUTTONDOWN, HTCAPTION, 0)`，交给系统的模态移动循环，所以有边缘吸附、
  跟手；**必须 Post 不能 Send**（Send 会一直阻塞到拖动结束，js_api 不返回、
  UI 线程被占死，表现是「按下去界面就僵了」）。
* 按钮、开关、设备行、滑块、Tab、图表等可交互元素**不会**触发拖动
  （`app.js` 的 `DRAG_BLOCK_SELECTOR` 排除）；滚动区内部的空白也不拖，
  避免想滚轮时把窗口拖跑。
* **鼠标移出窗口 2 秒自动收起**：判定在后端（`main.py _poll_cursor_leave()`），
  命令泵每 0.2 秒拿 `GetCursorPos` 与窗口矩形比一次，连续离开 ≥ 2 秒就
  `hide_window()`。早期版本靠前端 `document.mouseleave`，在快速移出、点击后、
  窗口失焦这些路径上不触发，表现就是「移开了也不收」。
* **位置不漂**：底边钉死在任务栏上沿往上长；`x` 算出后锁进 `_fixed_x` 复用；
  「已应用高度」与前端上报高度分开存，避免首帧误报把窗口压塌。
* **高度变化有过渡**：`set_height()` 不直接改窗口，而是把目标交给
  `main.py _anim_loop()` 的动画线程，180 ms ease-out cubic 逐帧 `SetWindowPos`
  （约 6 帧）。起跳高度读**真实窗口矩形**（`_real_height()`）而非目标值，
  中途换目标就从当前高度重新计时，不会堆线程。底边每帧都用
  `py = round((y+h)*scale) - ph` 反推，所以整个过程底边一动不动、只往上长。
* **DPI 倍率必须稳**：`dpi_scale()` 曾只走 `GetDC(0)` + `GetDeviceCaps` 且没
  声明 `argtypes`/`restype`，64 位下 HDC 被当 `c_int` 截断 → 偶发
  `OverflowError: int too long to convert` → 整个函数 except 返回 **1.0**。
  于是坐标按 1.0 算、`move_resize` 又乘 1.5，窗口就有一半跑到屏幕外（x 3228 /
  屏幕 2560）。现在优先用不碰句柄的 `GetDpiForSystem()`，`GetDeviceCaps` 只
  作兜底且补齐类型声明；`move_resize(clamp=True)` 再在 `SetWindowPos` 之前把
  矩形硬夹进显示器范围，保证任何上游算错都不会把窗口丢出屏幕。
* **溢出面板避让**：点 ^ 弹出的托盘溢出面板（类名
  `TopLevelWindowForOverflowXamlIsland`）也从任务栏往上长，会盖住窗口下半截。
  `winui.overflow_panel_rect()` 用 `IsWindowVisible` 判它是否弹出（这个窗口
  一直存在，只能看可见性），弹出时把底边改钉在面板上沿再减 `TASKBAR_MARGIN`，
  实测窗口从 `(1971,1263,2559,1515)` 上移到 `(1971,1092,2559,1344)`，
  底边 1344 / 面板顶 1357，留 13 物理像素间隙。`main.py _poll_panel_state()`
  在命令泵里每次比一次开合状态，一变就重排 —— 所以弹出瞬间就避让，而不是等
  下次显示；面板收回后同样自动落回原位。
* **托盘**：左键单击 = 显示窗口（不再切换）；右键菜单 = 立即刷新 / 开机自启 /
  退出。「显示」这一项已从菜单删掉，因为左键就是显示。
* **启动静默**：双击 `start.bat` 或 exe 都只在托盘出现，不弹窗口、不闪控制台
  （`start.bat` 用 `pythonw` + 全程无 `pause`，错误走 `logs\start.log` 和消息框）。
  想直接看界面用 `python main.py --show` 或 `WirelessStatus.exe --show`。
* **单实例**：第二次启动不会开第二个进程，而是给已有窗口发自定义消息把它唤起，
  然后自身退出。
* **退出**：真退出 —— 停采集线程、删托盘图标、销毁两个窗口、结束进程，
  任务管理器里不留残影。

---

## 七、电量精度：1% 不是设备能报 1%

这是本项目最容易误解的一点，单独说清楚。先回答问题：

**Q1：电量准不准？**

分三种情况，按设备**实际枚举到的特性**判断，不按型号猜：

| 设备支持 | 我们怎么做 | 界面标注 |
|---|---|---|
| `0x1000` batteryStatus（百分比） | 原样采用 | **设备上报** |
| `0x1001` batteryVoltage（毫伏） | 5 点中值滤波 + 曲线换算 | **估算** + 附 mV |
| `0x1004` unifiedBattery | 原样采用 | **设备上报** |
| 都不支持 | 不显示数字 | **不支持**（不是 0%） |

* 只有**真百分比**才是真的。电压换算是估算，误差约 ±5%，而且**移动瞬间
  会因为负载压降偏低** —— 所以做了中值滤波（取 5 个样本的中位数，不是均值；
  甩鼠标产生的压降是尖峰，中值能吃掉）。
* 曲线有两套：碱性（默认）和镍氢。改 `config.json` 的 `battery_curve`
  为 `"nimh"` 即可。镍氢的中段电压比碱性低，同一电压下换算结果更高。
* **请注意一个反直觉的事**：需求文本里写「0x1001 = 百分比 / 0x1000 = 电压」，
  但罗技官方规范和本机实测**正好相反**（`0x1000` 回 `0x5A`=90 的百分比，
  `0x1001` 才是毫伏）。所以 `battery.py` 不硬编码语义，而是「官方规范优先 +
  数值合理性兜底」：`0x1000` 优先当百分比、`0x1001` 优先当毫伏，数值不合常理
  时互换，都不合常理就显示「不支持」。

**Q2：是不是真的按 1% 计数？**

是 —— 但要说清楚「1% 是我们的**计数单位**，不代表设备能报这么细」。

* 内部统一用 **int 0–100**，**禁止** `//5*5`、`round(x/10)*10` 这类取整，
  CSS 也不按 5% 台阶画环（`conic-gradient` 用的是 `calc(var(--pct) * 1%)`）。
* UI 的判据是**整数不相等就重绘**，所以 90 → 89 一定看得见。
* 历史采样：变化 ≥ 1% 落一条；没变化时每 15 分钟也补一条心跳样（不然曲线会
  断）。保留 32 天，单设备上限 20000 条，超了按时间抽稀（等间隔保首尾）。
* **如果设备只报 5% 粒度**（比如只有 95/90/85 三个值），我们**不会伪造抖动**：
  `GranularityTracker` 会算出粒度，界面标「设备粒度 5%」，`raw_percent` 保留
  设备给的原始值。这时你看到的就是 5% 一跳，那是设备的限制，不是我们的。

---

## 八、已知限制

* 只支持 Windows（托盘用 pywin32，`GetLastInputInfo` / `GetCursorPos` 也是 Win32 API）。
* 单实例：已经在跑时再双击不会开第二个进程，只会把已有窗口唤起来。
* **多设备只按「能 ping 通」列出，不做逐个 probe**：HID++ 要一台台 ping + 读特性，
  轮询里只 probe 了当前那一台（这是刻意的性能取舍 —— 6 个索引全 probe 一轮要几秒）。
  所以非当前设备那一行可能只有「索引 + 未知型号」，点进去才补全。
* 不支持耳机/手柄的专属功能（音量、EQ、震动），只做「电量 + 存在感」。
  种类靠产品名关键词猜（`guess_kind`），猜不出来按鼠标渲染。
* 没有改回报率、按键映射、宏、灯效 —— 这个工具只做「看信息 + 改 DPI」。
* **深色模式不支持**：界面统一纯白一套。原本做了 `prefers-color-scheme` 两套配色，
  但无边框 + 透明窗在 WebView2 上拿不到真正的半透明玻璃效果（`transparent=True`
  不生效，实际是一块不透明矩形），深色下反而更难看，所以干脆只留浅色。
* **不在任务栏出现图标是刻意的**（`WS_EX_TOOLWINDOW`），所以 Alt+Tab 里也看不到它。
* **待机时间不展示**：`GetLastInputInfo` 只有全系统口径，没有逐设备口径，
  放在设备详情里会误导，所以整个功能删掉了。
* **「最后操作时间」不展示**：同理，它只反映「什么时候有键鼠动作」，对「这只
  鼠标用了多久」没有增量信息，v3 起从后端到前端全链路删除。
* **贴片嵌入 `Shell_TrayWnd` 依赖 Explorer**：如果任务栏被换成第三方实现
  （StartAllBack 之类的重皮肤工具、或 Explorer 重启过程中），`SetParent` 可能
  被拒。此时会自动降级成「任务栏内的置顶浮窗」，并在界面顶部给出提示。
* exe 没有签名，Windows SmartScreen 可能提示「未知发布者」，点「更多信息 →
  仍要运行」即可。

---

## 九、托盘图标不出现（`Shell_NotifyIcon` 拒绝访问）

症状：窗口一切正常，就是托盘区没有图标；`ui_error.log` 里有

```
tray: 创建失败 error: (5, 'Shell_NotifyIcon', '拒绝访问。')
```

**这不是代码问题，是进程完整性级别（Integrity Level）比任务栏低。**
explorer 跑在 Medium IL（`0x2000`），如果本程序因为**所在目录带低完整性
标签**而跑在 Low IL（`0x1000`），UIPI 会直接把这条跨进程窗口消息拦掉，
`Shell_NotifyIcon` 必然返回 error 5。

怎么确认：

```bat
:: 目录有没有低完整性标签？输出里出现 S-1-4-<一串数字> 就是有
icacls "D:\你的目录"
```

```
D:\你的目录 BUILTIN\Administrators:(I)(F)
            ...
            S-1-4-525062811-1021822693:(I)(OI)(CI)(W,D,DC)   ← 这一行
```

`S-1-4-` 是强制标签 SID 的根，这一行说明目录（及其子项）被打了低完整性标签，
从中启动的进程会继承 Low IL。

怎么解决：**把程序（或 `dist` 整个目录）放到普通目录**（桌面、文档、任意
非受限路径）再运行。本程序的开发工作区就有这个标签，所以在本工作区里直接
双击 `dist\WirelessStatus.exe` 一定没有托盘图标；拷到别处就正常——这不是
「打包坏了」，是运行位置的问题。

程序自身不会去绕过 UIPI（那需要提权或改安全策略，不合适），但**会把原因
显示出来**：窗口顶部会出现一条提示，说明「托盘图标创建失败 + 权限低于任务栏」，
而不是让你对着空托盘区猜。

---

## 十、exe 旁边冒出 `debug.log`（搜狗输入法写的，不是本程序的）

症状：程序一启动，exe 同目录（从资源管理器双击时就是那个目录）多出一个
`debug.log`，内容是：

```
[pid:tid:MMDD/HHMMSS.mmm:WARNING:t_skinfilemap.cpp(56)]
SGRenderLog:CheckUpdate newMem.OpenMem failed,
szNewMem:Local\sgime_skin.v2.filemap.sogouime err:系统错误2: 系统找不到指定的文件。
打开Filemap失败: Local\sgime_skin.v2.filemap.sogouime255893498a7
[pid:tid:MMDD/HHMMSS.mmm:WARNING:t_skinfilemap.cpp(65)]
SGRenderLog:CheckUpdate finish: old version:1
```

看着像本程序在报错，其实**跟本程序毫无关系**。

### 谁写的

搜狗输入法。它的 TSF 组件会注入到任何带窗口的进程里，实测在我们这棵进程树
的 WebView2 浏览器进程上能看到这些模块：

```
C:\WINDOWS\system32\SogouTSF.ime
C:\WINDOWS\system32\SogouPY.ime
C:\Program Files (x86)\SogouInput\15.9.0.2558\Resource.dll
C:\Program Files (x86)\SogouInput\Components\isgpet\1.0.2.3026\isgpet_bundle64.dll
```

它内部用 Chromium 的日志设施打警告（`SGRenderLog` / `t_skinfilemap.cpp`），
而 Chromium 在没指定 `--log-file` 时**默认把日志写到进程的当前工作目录**
的 `debug.log`。日志前缀 `[pid:tid:MMDD/HHMMSS.mmm:WARNING:file(line)]`
是 Chromium 的格式，不是 Python 的。

### 为什么不能靠关日志

试过但**无效**：

* `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--disable-logging`
* `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--log-level=3`
* `--log-file=NUL`

原因是 pywebview 在 `platforms/edgechromium.py:82` 里硬编码

```python
props.AdditionalBrowserArguments = '--disable-features=ElasticOverscroll'
```

这一句是**赋值**不是追加，会把环境变量整个盖掉。要改就得 patch pywebview
源码，升级即失效，不值当。

### 本程序的修法

**把进程的 CWD 挪到自己的运行时目录**（`main.py` 的
`prepare_working_directory()`）：

```
%LOCALAPPDATA%\WirelessStatus\runtime\
```

于是搜狗的 `debug.log` 落在这个目录里，exe 旁边永远干净。

为什么敢改 CWD：本程序所有文件访问都走 `apppath.app_dir()` /
`apppath.resource_dir()` 的绝对路径，全项目没有一处相对路径的 `open()`。

启动时还会顺手 `apppath.purge_cwd_junk(app_dir())` 清掉旧版本留在 exe 旁边的
同名垃圾，用户升级后不会再看到历史遗留文件。

### 验证

```bat
:: 启动程序，然后看它的 CWD
:: 期望：C:\Users\<你>\AppData\Local\WirelessStatus\runtime\
```

```python
# 用 PEB 读别的进程 CWD（见项目里 _cwdof.py 的做法）
# pythonw.exe pid=4388 cwd=C:\Users\<用户名>\AppData\Local\WirelessStatus\runtime\
```

同时 exe 目录里**不应该**有 `debug.log`。
