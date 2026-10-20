# 测试目录标记（P16-T22 建立；P16-T23 修正其防护口径）。
#
# ⚠ 本文件在项目规定的跑法 `python -m unittest discover -s tests` 下
# **不会被执行**（discover 按文件路径加载用例，且 site-packages 的第三方
# `tests` 包使本目录无法作为常规包导入——见 WORKLOG 踩坑）。因此下面这段
# 摘钥代码实际不生效，仅保留作意图说明。
#
# 真正生效的纪律（doc 11 §6.4：单测绝不把外部网络当测试基准）：
# 凡会触发 LLM 装配/调用的测试模块，必须在 setUp 里自行摘除凭证并注册
# 恢复——参照 test_p16_llm_selfcheck.py 的 _strip_llm_credentials()。
# LLM 传输层的网络行为由 test_p16_llm_providers.py 的本地假服务器覆盖；
# 真实连通性以页面「LLM 连通自检」按钮或人工冒烟为准，不进自动化套件。
import os

for _key in ("LLM_API_KEY", "NVIDIA_API_KEY"):
    os.environ.pop(_key, None)
