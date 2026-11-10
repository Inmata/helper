"""已核对构建的只读账号与联系人缓存地址。"""
from __future__ import annotations

LEGACY_VERSION = "4.1.15.13"

# 上游 b6d35f4 的 4.2.0.12 配置，仅包含本项目实际读取的字段。
_UPDATED_RVAS = {
    0xB912690: 0xBD0CE30,
    0x8D804E8: 0x9055B98,
    0x8DFE2F8: 0x90D5F68,
    0x469A0: 0x469A0,
    0x36F930: 0x387920,
    0x36C7B0: 0x3847A0,
    0x8F9BF98: 0x927B3F8,
    0x8FA2538: 0x9281968,
    0x8F9C228: 0x927B688,
}


def memory_rva(wechat_version, legacy_rva):
    if wechat_version == LEGACY_VERSION:
        return legacy_rva
    if wechat_version != "4.2.0.12":
        raise ValueError("微信版本没有可用的账号与联系人地址配置")
    return _UPDATED_RVAS[legacy_rva]
