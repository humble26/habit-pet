"""无控制台启动入口：开机自启与双击运行用（.pyw 由 pythonw 执行）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv = [sys.argv[0]]

from habitpet.__main__ import main

main()
