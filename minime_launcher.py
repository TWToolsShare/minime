"""pipx / pip 安装后的入口：定位代码目录并执行 run/minime.py。

仓库根目录直接运行时（python minime_launcher.py）也能用。
"""
import os
import runpy
import sys


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    code = os.path.join(root, "minime")
    if not os.path.isdir(os.path.join(code, "run")):
        code = root
    sys.path.insert(0, code)
    script = os.path.join(code, "run", "minime.py")
    sys.argv[0] = script
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
