# Sky PC MCP Companion

一个给 PC 版《Sky: Children of the Light / 光遇》用的本地 MCP 小工具。它把本机窗口截图、OCR 读屏、键盘输入和聊天输入包装成 MCP/JSON-RPC tool，方便支持 MCP 或 HTTP tool 的 AI 客户端在局域网内做陪聊和本地实验。

## 它做什么

- 识别 PC 版光遇窗口
- 截取游戏窗口并做 OCR
- 返回截图或 OCR 文本
- 发送键盘按键，例如 `w`、`space`、`f`、`q`、`enter`
- 通过剪贴板向聊天框粘贴中文/英文消息

它不读游戏内存，不修改客户端，不破解协议，也不提供刷资源、代肝或自动跑图功能。请只把它当成本地可访问性/陪聊实验使用。

## 安装

需要 Windows 10/11 和 Python **3.11**（推荐，本项目脚本默认按 3.11 配置；3.10 或更新版本也可以）。

### 第 0 步：安装 Python 3.11

1. 打开 <https://www.python.org/downloads/release/python-3119/>
2. 往下滚，找到 **Windows installer (64-bit)**，下载并运行
3. **一定要勾选底部的 “Add python.exe to PATH”**，然后点 Install Now
4. 装完后打开终端，输入 `python --version`，能显示 `Python 3.11.x` 就说明成功

### 第 1 步：下载本项目代码

方式一（新手推荐，不用装 Git）：

1. 打开 <https://github.com/sereinliovo-byte/sky-pc-mcp-companion>
2. 点绿色 `Code` → `Download ZIP`
3. 解压到桌面或任意英文路径文件夹

方式二（用 Git）：

```bat
git clone https://github.com/sereinliovo-byte/sky-pc-mcp-companion.git
cd sky-pc-mcp-companion
```

如果提示：

```text
git 不是内部或外部命令 / 无法将 "git" 识别为 cmdlet
```

说明电脑没有安装 Git。可以改用上面的 ZIP 方式，或者安装 Git for Windows 后重新打开终端。

### 第 2 步：安装依赖

```bat
python -m pip install -r requirements.txt
```

在代码文件夹空白处右键，选择“在终端中打开”或“Open in Terminal”，再运行上面的命令。
如果提示 `python` 不是命令，说明 Python 没装好或没加 PATH。重新安装 Python，并勾选 “Add python.exe to PATH”。

### 第 3 步：安装识图（OCR）工具（可选，二选一）

不装也能启动，只是“读屏幕文字”功能不可用。

- 方式一（推荐，中文识别效果好）：运行 `python -m pip install paddleocr paddlepaddle`，第一次启用时会自动下载识别模型（要联网，约几百 MB），等待完成即可。
- 方式二（轻量）：先安装 Tesseract 软件（<https://github.com/UB-Mannheim/tesseract/wiki>），再运行 `python -m pip install pytesseract`。

### 第 4 步：启动

双击 `start-http.bat`，或命令行运行：

```bat
python sky-mcp-server.py --http --host 0.0.0.0 --port 9800 --token li12345
```

启动后连接地址：`http://127.0.0.1:9800`（手机用这台电脑的局域网 IP），Token：`li12345`。

## 启动 HTTP MCP

双击：

```bat
start-http.bat
```

窗口会显示类似：

```text
URL:   http://0.0.0.0:9800
Token: 一串随机 token
```

手机或其他局域网客户端连接时，不要填 `0.0.0.0`，也不是填 GitHub 的地址。这里要填的是**运行 `start-http.bat` 那台电脑自己的局域网 IP**：

```text
http://192.168.1.23:9800
```

鉴权头：

```text
Authorization: Bearer 上面显示的Token
```

如果你的客户端把请求头拆成两格来填：

```text
Header name:  Authorization
Header value: Bearer 上面显示的Token
```

注意：

- `Bearer` 后面有一个英文空格。
- 不要加引号，不要换行，不要把 `Token:` 这个标签一起复制进去。
- `start-http.bat` 每次启动时如果没有设置 `SKY_MCP_TOKEN`，都会生成一串新的 token。重启过窗口就要复制当前窗口里最新的那一串。

在运行 MCP 的那台 Windows 电脑上打开命令行，输入：

```bat
ipconfig
```

找当前 Wi-Fi 或以太网下面的 `IPv4 地址`，通常长得像：

```text
192.168.x.x
10.x.x.x
172.16.x.x
```

例如查到 `IPv4 地址` 是 `192.168.1.23`，客户端里就填：

```text
http://192.168.1.23:9800
```

手机和电脑必须连在同一个 Wi-Fi / 同一个局域网里。

### 测试连接报 HTTP 401

如果客户端提示类似：

```text
HTTP 401 {"error": "missing or invalid bearer token"}
```

这说明手机已经连到这台电脑的 MCP 了，网络地址和端口大概率是通的；失败点在鉴权头。

按顺序检查：

1. 电脑上的 `start-http.bat` 窗口还开着，并且没有重启过。
2. 客户端里的 URL 是电脑局域网 IP，例如 `http://192.168.1.23:9800`，不是 `0.0.0.0`。
3. 自定义请求头名字是 `Authorization`。
4. 自定义请求头内容是 `Bearer 当前窗口显示的Token`。
5. 如果刚刚关闭又重新打开过 `start-http.bat`，复制新的 token。

也可以先在手机浏览器打开：

```text
http://电脑局域网IP:9800/health
```

如果能看到 `{"status":"ok"...}`，说明网络通；此时连接 MCP 仍然 401，就是 token 或请求头没有填对。

## 管理员模式

如果光遇、Steam、启动器或游戏平台是“以管理员身份运行”的，这个 MCP 也要用管理员权限启动。否则 Windows 可能允许截图，但不允许普通权限的 Python 给管理员权限窗口发送按键。

管理员启动方式：

1. 右键 `start-http.bat`
2. 选择“以管理员身份运行”
3. 保持弹出的命令行窗口不要关闭

如果右键 `.bat` 没有管理员选项，可以先用管理员权限打开命令提示符：

```bat
cd /d C:\path\to\sky-pc-mcp-companion
start-http.bat
```

如果 `read_screen` / `screenshot` 正常，但 `press_key` 完全不动，优先试管理员模式。

## 本地 stdio MCP

如果你的 MCP 客户端支持 stdio，可以使用：

```bat
start-stdio.bat
```

或直接配置：

```json
{
  "command": "python",
  "args": ["C:\\path\\to\\sky-pc-mcp-companion\\sky-mcp-server.py"]
}
```

## 常用 tool

### status

检查依赖、OCR、输入后端和窗口识别。

```json
{
  "name": "status",
  "arguments": {}
}
```

如果 `window` 是 `null`，说明没有找到游戏窗口。可以把游戏切到窗口化/无边框窗口化，或用真实窗口标题启动：

```bat
python sky-mcp-server.py --http --host 0.0.0.0 --port 9800 --token 你的token --window-title "窗口标题的一部分"
```

### read_screen

截图并 OCR 当前游戏窗口。

```json
{
  "name": "read_screen",
  "arguments": {}
}
```

### screenshot

返回当前游戏窗口截图。

```json
{
  "name": "screenshot",
  "arguments": {}
}
```

### press_key

发送按键。移动测试建议先用长一点的按压时间。

```json
{
  "name": "press_key",
  "arguments": {
    "key": "w",
    "duration_ms": 800,
    "backend": "pydirectinput"
  }
}
```

如果 `pydirectinput` 不动，可以试：

```json
{
  "name": "press_key",
  "arguments": {
    "key": "w",
    "duration_ms": 800,
    "backend": "pyautogui"
  }
}
```

### send_chat

打开聊天框、粘贴消息并发送。

```json
{
  "name": "send_chat",
  "arguments": {
    "message": "你好，我是本地 AI 陪聊测试",
    "backend": "pydirectinput"
  }
}
```

### type_text

如果聊天框已经手动打开，可以只粘贴文本：

```json
{
  "name": "type_text",
  "arguments": {
    "message": "测试中文输入",
    "send": true,
    "backend": "pydirectinput"
  }
}
```

## 能聚焦游戏，但不能输入文字

如果 AI 客户端能把光遇窗口拉到前台，但发不出字，说明窗口识别和聚焦已经成功了，问题通常在“打开聊天框 / 剪贴板粘贴 / 模拟 Enter”这条输入链。

先分开测：

1. 让客户端调用 `open_chat`。如果只聚焦窗口但聊天框没打开，分别试 `backend: pydirectinput` 和 `backend: pyautogui`，并确认光遇窗口不是管理员权限而 MCP 普通权限。
2. 手动在游戏里打开聊天输入框，再让客户端调用 `type_text`。如果这时能输入，说明问题在自动打开聊天框，不在粘贴。
3. 如果手动打开聊天框后 `type_text` 也不能输入，优先检查管理员权限和输入后端。
4. 如果光遇、Steam、启动器或游戏平台是管理员权限运行，`start-http.bat` 也要右键“以管理员身份运行”。
5. 如果模型只调用了 `focus_game`，它只会聚焦窗口，不会输入文字。要让它调用 `send_chat`，或者在聊天框已打开时调用 `type_text`。

推荐给 AI 客户端的测试顺序：

```json
{"name":"open_chat","arguments":{"backend":"pydirectinput"}}
```

聊天框打开后：

```json
{"name":"type_text","arguments":{"message":"hello","send":true,"backend":"pydirectinput"}}
```

如果不行，再把两个请求里的 `backend` 改成 `pyautogui` 试一次。

## 按键没反应

如果能截图、能 OCR，但是角色不动，说明 MCP 已经连上了，问题通常在 Windows 输入层。

按顺序试：

1. 手动点一下光遇窗口内部，再测 `press_key`。
2. 把光遇切到窗口化或无边框窗口化。
3. 右键“以管理员身份运行” `start-http.bat`，尤其是光遇或 Steam 本身用管理员权限启动时。
4. 分别试 `backend: pydirectinput` 和 `backend: pyautogui`。
5. 如果截图正常但两个后端都不能动，说明这台电脑上的光遇拦截了模拟输入，需要更底层的虚拟键盘/虚拟手柄方案。

## 安全边界

- 不要把端口暴露到公网。
- 不要把 token 发给不信任的人。
- 截图和 OCR 可能包含聊天内容，不要公开上传调试日志。
- 不建议做自动刷资源、自动跑图、代肝、骚扰等用途。
- 如果游戏或平台规则不允许自动化，请停止使用。
