"""数据来源适配器：公司清单里的 source -> 模块。接口见 base.py。

还没做好的来源在这里没有登记，框架会跳过对应公司并在报告里说明；做好以后在 ADAPTERS 里加一行即可。
hkex（港交所披露易）、cninfo（巨潮资讯）共用 cn_pdf.py（读定期报告 PDF；只有这两个适配器用 pypdf）。
"""
from . import cninfo, hkex, lilly_deep, sec

ADAPTERS = {
    "lilly_deep": lilly_deep,
    "sec": sec,
    "hkex": hkex,
    "cninfo": cninfo,
}


def get(source):
    return ADAPTERS.get(source)
