#pragma once
#include <vector>

// 语音构造和提交来自上游 helper；音频从本项目的请求缓冲区直接传入。
struct VoiceSendConfig {
    TextConfig base;
    char account[128];
    const char* audio;
    uint32_t audio_size, duration_ms;
};
static_assert(sizeof(VoiceSendConfig) == 1832, "Unexpected voice send ABI");

struct VoiceSendLease {
    VoiceSendConfig config;
    std::vector<char> bytes;
    explicit VoiceSendLease(const VoiceSendConfig& value) : config(value), bytes(value.audio_size + 1) {
        _InterlockedIncrement(&live_tasks);
    }
    ~VoiceSendLease() { _InterlockedDecrement(&live_tasks); }
};

static bool copy_voice_audio(char* output, const VoiceSendConfig& config) {
    __try {
        std::memcpy(output, config.audio, config.audio_size);
        return !std::memcmp(output, "\x02#!SILK_V3", 10);
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static SmallString voice_borrow_string(const char* bytes, size_t length) {
    SmallString view{};
    view.length = length;
    view.capacity = length <= 15 ? 15 : length;
    if (length <= 15) std::memcpy(view.data, bytes, length + 1);
    else std::memcpy(view.data, &bytes, sizeof(bytes));
    return view;
}

static bool voice_account_matches(const SharedMessage& context, const char* account) {
    const auto root = *reinterpret_cast<const uintptr_t*>(core_address(core_base, 0xb912690));
    if (!root || *reinterpret_cast<const uintptr_t*>(root) != core_address(core_base, 0x8d804e8) ||
        !context.object || *reinterpret_cast<void* const*>(root + 0x68) != context.object ||
        *static_cast<const uintptr_t*>(context.object) != core_address(core_base, 0x8dfe2f8))
        return false;
    const auto identity = reinterpret_cast<const SmallString*>(
        static_cast<const unsigned char*>(context.object) + 0x48);
    const auto length = std::strlen(account);
    if (identity->length != length || identity->capacity < length) return false;
    const char* bytes = identity->capacity > 15 ?
        *reinterpret_cast<const char* const*>(identity->data) : identity->data;
    return bytes && !std::memcmp(bytes, account, length);
}

static void send_voice_on_task(const VoiceSendLease& lease) {
    const auto& config = lease.config;
    SharedMessage context{}, provider{}, manager{}, message{};
    alignas(16) unsigned char result[128]{};
    auto target = voice_borrow_string(config.base.target, std::strlen(config.base.target));
    auto payload = voice_borrow_string(lease.bytes.data(), config.audio_size);
    // 沿用微信自带录音发送的语音对象选项。
    alignas(8) unsigned char options[56]{};
    *reinterpret_cast<uint32_t*>(options) = 4;
    *reinterpret_cast<uint32_t*>(options + 4) = 3;
    *reinterpret_cast<uint64_t*>(options + 8) = 30000;
    *reinterpret_cast<uint64_t*>(options + 48) = 15;
    auto duration = config.duration_ms;
    TestResult report{};
    char client_message_id[37]{};
    auto& result_message = *reinterpret_cast<SharedMessage*>(result + 40);
    report.native_status = 0xffffffffu;
    __try {
        report.stage = 60;
        using GetContext = SharedMessage* (__fastcall*)(SharedMessage*);
        using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
        reinterpret_cast<GetContext>(core_address(core_base, 0x47220))(&context);
        if (!voice_account_matches(context, config.account)) __leave;

        report.stage = 61;
        using Factory = SharedMessage* (__fastcall*)(SharedMessage*, const SmallString*,
                                                    const SmallString*, const uint32_t*, const void*);
        reinterpret_cast<Factory>(core_address(core_base, 0x6be0020))(&message, &target, &payload, &duration, options);
        if (!message.object || !message.control ||
            *static_cast<const uintptr_t*>(message.object) != core_address(core_base, 0x9af9078))
            __leave;
        auto object = static_cast<unsigned char*>(message.object);
        *reinterpret_cast<uint32_t*>(object + 0x118) = 34;
        if (reinterpret_cast<const SmallString*>(object + 0x758)->length != payload.length ||
            *reinterpret_cast<const uint32_t*>(object + 0x778) != duration) __leave;

        report.stage = 63;
        reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(context.object, &provider);
        if (!provider.object || !provider.control) __leave;
        reinterpret_cast<GetService>(core_address(core_base, 0x6f4d90))(provider.object, &manager);
        if (!manager.object || !manager.control || !voice_account_matches(context, config.account)) __leave;
        SharedMessage transferred = message;
        _InterlockedIncrement(reinterpret_cast<volatile LONG*>(static_cast<unsigned char*>(message.control) + 8));
        reinterpret_cast<SendFunction>(core_address(core_base, 0x178f100))(manager.object, result, &transferred, 1);
        report.native_status = *reinterpret_cast<const uint32_t*>(result);
        report.native_status_high = *reinterpret_cast<const uint32_t*>(result + 4);
        report.stage = 64;
        // 发送回执先给出客户端 UUID；引用卡片的 clientmsgid 末尾携带同一个 UUID。
        if (core_profile_id == 4 && !report.native_status && !report.native_status_high && result_message.object) {
            auto receipt = static_cast<const unsigned char*>(result_message.object);
            const auto id = reinterpret_cast<const SmallString*>(receipt + 0x30);
            if (receipt[0x50] == 1 && id->length == 36)
                std::memcpy(client_message_id, *reinterpret_cast<const char* const*>(id->data), 36);
        }
    } __except (record_exception(GetExceptionInformation(), &report)) {}

    release_image_object(result_message, &report);
    release_image_string(reinterpret_cast<SmallString*>(result + 8), &report);
    release_image_object(message, &report);
    release_image_object(manager, &report);
    release_image_object(provider, &report);
    release_image_object(context, &report);
    FILE* log = nullptr;
    if (_wfopen_s(&log, config.base.base.log_path, L"wb") == 0 && log) {
        std::fprintf(log, "{\"operation\":\"voice_send\",\"message_type\":34,\"duration_ms\":%u,"
                     "\"bytes\":%u,\"stage\":%lu,\"exception\":%lu,\"native_status\":%u,\"target\":",
                     duration, config.audio_size, report.stage, report.exception, report.native_status);
        write_json_string(log, config.base.target);
        if (client_message_id[0]) {
            std::fputs(",\"client_message_id\":", log);
            write_json_string(log, client_message_id);
        }
        std::fputs(",\"text\":", log);
        write_json_string(log, config.base.base.text);
        std::fprintf(log, ",\"completion_success\":%s}\n",
                     report.stage == 64 && !report.exception && !report.native_status &&
                     !report.native_status_high ? "true" : "false");
        std::fclose(log);
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxSendVoice(void* parameter) {
    if (!parameter || live_tasks || live_completions) return ERROR_BUSY;
    const VoiceSendConfig config = *static_cast<const VoiceSendConfig*>(parameter);
    if (config.base.base.size != sizeof(config) || config.base.base.pid != GetCurrentProcessId() ||
        !valid_target(config.base.target) || !valid_target(config.account) || !config.audio ||
        config.audio_size <= 12 || config.audio_size > 20 * 1024 * 1024 ||
        !config.duration_ms || config.duration_ms > 60000 || config.duration_ms % 20)
        return ERROR_INVALID_PARAMETER;
    const HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    try {
        auto lease = std::make_shared<VoiceSendLease>(config);
        // 请求线程退出后 Python 即可释放输入；排队的发送只读自己持有的副本。
        if (!copy_voice_audio(lease->bytes.data(), config)) return ERROR_INVALID_DATA;
        std::function<void()> task = [lease]() { send_voice_on_task(*lease); };
        return schedule_task(config.base.base, &task);
    } catch (const std::bad_alloc&) { return ERROR_NOT_ENOUGH_MEMORY; }
}
