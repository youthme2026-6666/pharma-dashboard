"""数据来源适配器：公司清单里的 source -> 模块。接口见 base.py。

还没做好的来源（sec、hkex、cninfo）在这里没有登记，框架会跳过对应公司并在报告里说明；
做好以后在 ADAPTERS 里加一行即可。
"""
from . import lilly_deep

ADAPTERS = {
    "lilly_deep": lilly_deep,
}


def get(source):
    return ADAPTERS.get(source)
