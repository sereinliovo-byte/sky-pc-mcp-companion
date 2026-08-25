# -*- coding: utf-8 -*-
import os
import subprocess
import sys


def main() -> int:
    frozen = getattr(sys, "frozen", False)
    if frozen:
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    server = os.path.join(base_dir, "sky-mcp-server.py")
    if not os.path.exists(server):
        server = os.path.join(os.getcwd(), "sky-mcp-server.py")
    if not os.path.exists(server):
        print("错误：找不到 sky-mcp-server.py")
        print("请把这个程序放在 sky-mcp-server.py 同一个文件夹里再启动。")
        input("按回车退出...")
        return 1

    python_candidates = [
        r"C:\Users\16348\AppData\Local\Programs\Python\Python311\python.exe",
        "python",
    ]
    python = "python"
    for candidate in python_candidates:
        if candidate == "python" or os.path.exists(candidate):
            python = candidate
            break

    cmd = [
        python,
        server,
        "--http",
        "--host", "0.0.0.0",
        "--port", "9800",
        "--token", "li12345",
    ]
    print("Sky MCP Server 一键启动")
    print("-------------------------------")
    print("URL:   http://0.0.0.0:9800")
    print("Token: li12345")
    print()
    print("手机/客户端请用这台电脑的局域网 IP 连接，例如 http://192.168.x.x:9800")
    print("关闭这个窗口 = 停止服务器")
    print("-------------------------------")
    try:
        subprocess.run(cmd, cwd=base_dir)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print("启动失败：", exc)
    print()
    input("服务器已退出，按回车关闭窗口...")
    return 0


if __name__ == "__main__":
    sys.exit(main())