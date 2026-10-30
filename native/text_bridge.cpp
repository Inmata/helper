// 通用文本桥接。复用已经实测的版本指纹、字符串分配器、任务调度和卸载保护。
// 单独生成 text_bridge.dll，不替换仍在运行的 selftest_bridge.dll。
#define WXBOT_EXTENDED_RECEIVE
#include "selftest_bridge.cpp"
#include "image_download.h"

struct TextConfig {
    TestConfig base;
    char target[128];
};
static_assert(sizeof(TextConfig) == 1688, "Unexpected text config ABI");

struct AtTextConfig { TextConfig base; char members[1024]; };
static_assert(sizeof(AtTextConfig) == 2712, "Unexpected at-text config ABI");

struct MentionContext { const char* members; bool applied; };
using MessageFactory = SharedMessage* (__fastcall*)(SharedMessage*);
static MessageFactory original_factory = nullptr;

static SharedMessage* __fastcall mention_factory(SharedMessage* output) {
    _InterlockedIncrement(&active_callbacks);
    SharedMessage* result = nullptr;
    __try {
        result = original_factory(output);
        const auto context = factory_tls == TLS_OUT_OF_INDEXES ? nullptr :
            static_cast<MentionContext*>(TlsGetValue(factory_tls));
        if (context) {
            auto object = output ? static_cast<unsigned char*>(output->object) : nullptr;
            if (!object || *reinterpret_cast<uintptr_t*>(object) != core_base + 0x8e53588)
                RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
            // 实机真正艾特样本：最后两个 std::string 分别为正文 +0x758、成员列表 +0x778。
            auto members = reinterpret_cast<SmallString*>(object + 0x778);
            if (members->length != 0 || members->capacity != 15 ||
                !set_native_string(members, context->members))
                RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
            context->applied = true;
        }
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

static DWORD ensure_mention_factory() {
    if (factory_hook_created) return 0;
    MH_STATUS status = MH_Initialize();
    const bool initialized_here = status == MH_OK;
    if (status != MH_OK && status != MH_ERROR_ALREADY_INITIALIZED) return 600 + status;
    factory_tls = TlsAlloc();
    if (factory_tls == TLS_OUT_OF_INDEXES) {
        if (initialized_here) MH_Uninitialize();
        return ERROR_NOT_ENOUGH_MEMORY;
    }
    void* target = reinterpret_cast<void*>(core_base + 0x6f4c70);
    status = MH_CreateHook(target, reinterpret_cast<void*>(&mention_factory), reinterpret_cast<void**>(&original_factory));
    if (status == MH_OK) {
        status = MH_EnableHook(target);
        if (status != MH_OK) MH_RemoveHook(target);
    }
    if (status != MH_OK) {
        TlsFree(factory_tls); factory_tls = TLS_OUT_OF_INDEXES;
        if (initialized_here) MH_Uninitialize();
        return 600 + status;
    }
    factory_hook_created = true;
    return 0;
}

static bool valid_target(const char* target) {
    size_t length = strnlen_s(target, 128);
    if (!length || length >= 128) return false;
    for (size_t index = 0; index < length; ++index) {
        unsigned char ch = target[index];
        if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
              (ch >= '0' && ch <= '9') || ch == '_' || ch == '.' || ch == '@' || ch == '-')) return false;
    }
    return true;
}

struct TextCompletion {
    volatile LONG references;
    uint32_t padding;
    void (__fastcall* invoke)(TextCompletion*, const unsigned char*);
    void (__fastcall* destroy)(TextCompletion*);
    TextConfig config;
    char members[1024];
};

static void __fastcall text_completion_invoke(TextCompletion* self, const unsigned char* result) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        FILE* log = nullptr;
        bool success = result && result[0] == 1;
        if (_wfopen_s(&log, self->config.base.log_path, L"wb") == 0 && log) {
            std::fprintf(log, "{\"stage\":21,\"exception\":0,\"native_status\":%u,\"target\":", success ? 0 : 1);
            write_json_string(log, self->config.target);
            std::fputs(",\"text\":", log);
            write_json_string(log, self->config.base.text);
            std::fputs(",\"at_members\":", log);
            write_json_string(log, self->members);
            std::fprintf(log, ",\"thread_id\":%lu,\"completion_success\":%s}\n",
                         GetCurrentThreadId(), success ? "true" : "false");
            std::fclose(log);
        }
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static void __fastcall text_completion_destroy(TextCompletion* self) {
    delete self;
    _InterlockedDecrement(&live_completions);
}

static DWORD send_text_on_task(const TextConfig* config, const char* members = nullptr) {
    if (!config || live_completions != 0) return ERROR_BUSY;
    struct TextRequest { SmallString receiver; SmallString body; } request{};
    request.receiver.capacity = request.body.capacity = 15;
    SmallString description{};
    description.capacity = 15;
    if (!set_native_string(&request.receiver, config->target) ||
        !set_native_string(&request.body, config->base.text) ||
        !set_inline(&description, "wxbot.text")) {
        free_native_string(&request.receiver);
        free_native_string(&request.body);
        return ERROR_INVALID_PARAMETER;
    }
    TextCompletion* completion = new TextCompletion{1, 0, text_completion_invoke, text_completion_destroy, *config, {}};
    if (members) strcpy_s(completion->members, members);
    _InterlockedIncrement(&live_completions);
    TestResult report{};
    MentionContext mention{members, false};
    if (members && !TlsSetValue(factory_tls, &mention)) {
        text_completion_destroy(completion);
        free_native_string(&request.receiver); free_native_string(&request.body);
        return ERROR_INVALID_DATA;
    }
    __try {
        using Builder = void (__fastcall*)(void*, TextRequest*, SmallString*, TextCompletion**);
        reinterpret_cast<Builder>(core_base + 0x54b02f0)(nullptr, &request, &description, &completion);
        report.stage = 20;
    } __except (record_exception(GetExceptionInformation(), &report)) {
        write_report(config->base, report, "WXBOT_TEXT_SEND_EXCEPTION");
    }
    if (members) TlsSetValue(factory_tls, nullptr);
    if (completion) text_completion_destroy(completion);
    // 好友和群 ID 通常超过 SSO 的 15 字节，必须释放两份请求字符串。
    free_native_string(&request.receiver);
    free_native_string(&request.body);
    return report.exception ? 5 : 0;
}

static bool valid_mentions(const char* members) {
    size_t length = strnlen_s(members, 1024);
    if (!length || length >= 1024) return false;
    unsigned int count = 1;
    size_t part = 0;
    for (size_t index = 0; index < length; ++index) {
        unsigned char ch = members[index];
        if (ch == ',') {
            if (!part || part >= 128 || ++count > 8) return false;
            part = 0;
        } else {
            if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
                  (ch >= '0' && ch <= '9') || ch == '_' || ch == '.' || ch == '-')) return false;
            ++part;
        }
    }
    return part > 0 && part < 128;
}

struct AtTaskLease {
    AtTextConfig config;
    explicit AtTaskLease(const AtTextConfig& value) : config(value) { _InterlockedIncrement(&live_tasks); }
    ~AtTaskLease() { _InterlockedDecrement(&live_tasks); }
};

extern "C" __declspec(dllexport) DWORD WINAPI WxSendAtText(void* parameter) {
    if (!parameter || live_completions != 0 || live_tasks != 0) return ERROR_BUSY;
    const AtTextConfig config = *static_cast<const AtTextConfig*>(parameter);
    size_t target_length = strnlen_s(config.base.target, sizeof(config.base.target));
    if (config.base.base.size != sizeof(AtTextConfig) || config.base.base.pid != GetCurrentProcessId() ||
        !valid_target(config.base.target) || target_length < 9 ||
        std::strcmp(config.base.target + target_length - 9, "@chatroom") || !valid_mentions(config.members))
        return ERROR_INVALID_PARAMETER;
    size_t length = strnlen_s(config.base.base.text, sizeof(config.base.base.text));
    if (!length || length >= sizeof(config.base.base.text) ||
        !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.base.base.text, static_cast<int>(length), nullptr, 0))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    DWORD status = ensure_mention_factory();
    if (status != 0) return status;
    auto lease = std::make_shared<AtTaskLease>(config);
    std::function<void()> task = [lease]() {
        _InterlockedIncrement(&active_callbacks);
        __try { send_text_on_task(&lease->config.base, lease->config.members); }
        __finally { _InterlockedDecrement(&active_callbacks); }
    };
    status = schedule_task(config.base.base, &task);
    return status;
}

struct TextTaskLease {
    TextConfig config;
    explicit TextTaskLease(const TextConfig& value) : config(value) { _InterlockedIncrement(&live_tasks); }
    ~TextTaskLease() { _InterlockedDecrement(&live_tasks); }
};

extern "C" __declspec(dllexport) DWORD WINAPI WxSendText(void* parameter) {
    if (!parameter || live_completions != 0 || live_tasks != 0) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId() ||
        !valid_target(config.target)) return ERROR_INVALID_PARAMETER;
    size_t length = strnlen_s(config.base.text, sizeof(config.base.text));
    if (!length || length >= sizeof(config.base.text) ||
        !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.base.text, static_cast<int>(length), nullptr, 0))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    auto lease = std::make_shared<TextTaskLease>(config);
    std::function<void()> task = [lease]() {
        _InterlockedIncrement(&active_callbacks);
        __try { send_text_on_task(&lease->config); }
        __finally { _InterlockedDecrement(&active_callbacks); }
    };
    return schedule_task(config.base, &task);
}

static bool try_read_source(const unsigned char* message, size_t offset, char* output, size_t capacity) {
    __try {
        // 真正艾特小号样本已核对：msgsource +0x38 是直接 std::string 指针。
        // 与 from/to/content 的 StringValue 包装不同。
        const auto string = *reinterpret_cast<const SmallString* const*>(message + offset);
        if (!string || !string->length || string->length >= capacity || string->capacity < string->length ||
            !(string->capacity == 15 || (string->capacity >= 16 && string->capacity <= 1024 * 1024))) return false;
        const char* bytes = string->capacity > 15 ? *reinterpret_cast<const char* const*>(string->data) : string->data;
        std::memcpy(output, bytes, static_cast<size_t>(string->length));
        output[string->length] = 0;
        return std::strstr(output, "<msgsource") || std::strstr(output, "<atuserlist");
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static void sample_all_text(const uintptr_t* batch) {
    __try {
        if (!batch || batch[1] < batch[0] || (batch[1] - batch[0]) % 0x78 != 0 ||
            (batch[1] - batch[0]) / 0x78 > 512) return;
        for (uintptr_t address = batch[0]; address < batch[1]; address += 0x78) {
            const auto message = reinterpret_cast<const unsigned char*>(address);
            const uint32_t type = *reinterpret_cast<const uint32_t*>(message + 0x14);
            // 1 是文本；3 是图片；49 只放行带 refermsg 的引用消息，
            // 链接、文件、转发等其他卡片不要。
            if (type != 1 && type != 3 && type != 49) continue;
            char sender[128]{}, receiver[128]{};
            if (!read_pb_string(message, 8, sender, sizeof(sender)) || !valid_target(sender) ||
                !read_pb_string(message, 0x18, receiver, sizeof(receiver)) || !valid_target(receiver)) continue;
            // 接收瞬间复制元数据，不持有任何微信消息对象或字符串指针。
            char source[4096]{};
            if (!try_read_source(message, 0x38, source, sizeof(source))) source[0] = 0;
            // Sync AddMsg +0x50 -> 消息对象 +0x148；保留 64 位服务器编号，
            // 供图片事件与引用消息后续按原始编号关联。
            const uint64_t message_id = *reinterpret_cast<const uint64_t*>(message + 0x50);
            if (type == 3) {
                // 群图片正文同样是“成员 ID:\n<msg><img .../></msg>”，由 Python 路由剥离前缀。
                char image_xml[16384]{};
                if (!read_pb_string(message, 0x20, image_xml, sizeof(image_xml))) continue;
                enqueue_received(sender, receiver, "", source, 3, image_xml, message_id);
                continue;
            }
            char text[8192]{};
            if (!read_pb_string(message, 0x20, text, sizeof(text))) continue;
            if (type == 49 && !std::strstr(text, "<refermsg")) continue;
            enqueue_received(sender, receiver, text, source, type, nullptr, message_id);
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        // 布局不匹配时放弃采样，真实客户端仍按原逻辑收消息。
    }
}

static uintptr_t __fastcall text_receive(void* manager, const uintptr_t* batch, uint8_t flag1, uint8_t flag2) {
    _InterlockedIncrement(&active_callbacks);
    uintptr_t result = 0;
    __try {
        sample_all_text(batch);
        result = original_receive(manager, batch, flag1, flag2);
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxStartTextReceiver(void* parameter) {
    if (!parameter || hook_created || receive_hook_created) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId()) return 3;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    AcquireSRWLockExclusive(&receive_lock);
    receive_head = receive_count = receive_dropped = 0;
    receive_sequence = 0;
    ReleaseSRWLockExclusive(&receive_lock);
    MH_STATUS status = MH_Initialize();
    if (status != MH_OK) return 600 + status;
    void* target = reinterpret_cast<void*>(core_base + 0x17b9010);
    status = MH_CreateHook(target, reinterpret_cast<void*>(&text_receive), reinterpret_cast<void**>(&original_receive));
    if (status != MH_OK) { MH_Uninitialize(); return 600 + status; }
    status = MH_EnableHook(target);
    if (status != MH_OK) { MH_RemoveHook(target); MH_Uninitialize(); return 600 + status; }
    receive_hook_created = true;
    return 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollTextMessage(void* parameter) {
    // 保留旧接口的 1312 字节边界，旧调用者不会被 V2/V4 写越界。
    if (!parameter) return ERROR_INVALID_PARAMETER;
    ReceivedMediaMessageV4 message{};
    const DWORD code = poll_received(&message);
    copy_legacy_message(message, static_cast<LegacyReceivedMessage*>(parameter));
    return code;
}

// V2 调用方只分配 5408 字节（正文 1KB + 艾特来源 4KB）。队列内部是 28968 字节的
// V4 结构，所以这里先取到本地再按旧布局截断复制，绝不能把新结构直接写进旧缓冲区。
struct LegacyReceivedText {
    uint32_t size, present, type, thread_id, dropped, reserved;
    uint64_t sequence;
    char sender[128], receiver[128], text[1024], mention_source[4096];
};
static_assert(sizeof(LegacyReceivedText) == 5408, "Unexpected V2 receive ABI");

static void copy_legacy_text(const ReceivedMediaMessageV4& source, LegacyReceivedText* output) {
    output->size = sizeof(*output);
    output->present = source.present;
    output->type = source.type;
    output->thread_id = source.thread_id;
    output->dropped = source.dropped;
    output->reserved = source.reserved;
    output->sequence = source.sequence;
    std::memcpy(output->sender, source.sender, sizeof(output->sender));
    std::memcpy(output->receiver, source.receiver, sizeof(output->receiver));
    std::memcpy(output->text, source.text, sizeof(output->text) - 1);
    output->text[sizeof(output->text) - 1] = 0;
    std::memcpy(output->mention_source, source.mention_source, sizeof(output->mention_source) - 1);
    output->mention_source[sizeof(output->mention_source) - 1] = 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollTextMessageV2(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    ReceivedMediaMessageV4 message{};
    const DWORD code = poll_received(&message);
    if (code) return code;
    copy_legacy_text(message, static_cast<LegacyReceivedText*>(parameter));
    return 0;
}

// V3 调用方分配 12576 字节（正文 8KB + 艾特来源）；不含图片 XML 与原始消息编号。
struct LegacyReceivedTextV3 {
    uint32_t size, present, type, thread_id, dropped, reserved;
    uint64_t sequence;
    char sender[128], receiver[128], text[8192], mention_source[4096];
};
static_assert(sizeof(LegacyReceivedTextV3) == 12576, "Unexpected V3 receive ABI");

extern "C" __declspec(dllexport) DWORD WINAPI WxPollTextMessageV3(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    ReceivedMediaMessageV4 message{};
    const DWORD code = poll_received(&message);
    if (code) return code;
    auto output = static_cast<LegacyReceivedTextV3*>(parameter);
    output->size = sizeof(*output);
    output->present = message.present;
    output->type = message.type;
    output->thread_id = message.thread_id;
    output->dropped = message.dropped;
    output->reserved = message.reserved;
    output->sequence = message.sequence;
    std::memcpy(output->sender, message.sender, sizeof(output->sender));
    std::memcpy(output->receiver, message.receiver, sizeof(output->receiver));
    std::memcpy(output->text, message.text, sizeof(output->text) - 1);
    output->text[sizeof(output->text) - 1] = 0;
    std::memcpy(output->mention_source, message.mention_source, sizeof(output->mention_source) - 1);
    output->mention_source[sizeof(output->mention_source) - 1] = 0;
    return 0;
}

// V4 媒体队列：正文 8KB、艾特来源 4KB、图片 XML 16KB、64 位原始消息编号。
extern "C" __declspec(dllexport) DWORD WINAPI WxPollMediaMessageV4(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    _InterlockedIncrement(&active_callbacks);
    auto output = static_cast<ReceivedMediaMessageV4*>(parameter);
    const DWORD code = poll_received(output);
    _InterlockedDecrement(&active_callbacks);
    return code;
}

// 定向只读探针：仅在手动发送 WXBOT_AT_PROBE 的消息中记录 msgsource 字符串。
// 不发送消息、不改写消息对象，用于核对该版本真实艾特的元数据布局。
static TextConfig mention_probe_config{};

static bool copy_small_string(const unsigned char* object, size_t offset, char* output, size_t size) {
    __try {
        const auto string = reinterpret_cast<const SmallString*>(object + offset);
        if (!string->length || string->length >= size || string->capacity < string->length ||
            !(string->capacity == 15 || (string->capacity >= 16 && string->capacity <= 1024 * 1024))) return false;
        const char* data = string->capacity == 15 ? string->data : *reinterpret_cast<const char* const*>(string->data);
        std::memcpy(output, data, static_cast<size_t>(string->length));
        output[string->length] = 0;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static void capture_mention_source(SharedMessage* message) {
    __try {
        if (!message || !message->object) return;
        const auto object = static_cast<const unsigned char*>(message->object);
        if (*reinterpret_cast<const uintptr_t*>(object) != core_base + 0x8e53588) return;
        char body[1024]{};
        if (!copy_small_string(object, 0x758, body, sizeof(body)) || !std::strstr(body, "WXBOT_AT_PROBE")) return;
        if (_InterlockedCompareExchange(&triggered_once, 1, 0) != 0) return;
        FILE* log = nullptr;
        if (_wfopen_s(&log, mention_probe_config.base.log_path, L"wb") != 0 || !log) return;
        std::fputs("{\"vtable_rva\":\"0x8e53588\",\"fields\":[", log);
        bool comma = false;
        for (size_t offset = 0; offset + sizeof(SmallString) <= 0x798; offset += 8) {
            char value[4096]{};
            if (!copy_small_string(object, offset, value, sizeof(value))) continue;
            if (!std::strstr(value, "msgsource") && !std::strstr(value, "atuserlist") &&
                !std::strstr(value, "WXBOT_AT_PROBE") && !std::strstr(value, "wxid_") &&
                !std::strstr(value, "@chatroom")) continue;
            if (comma) std::fputc(',', log);
            std::fprintf(log, "{\"offset\":%llu,\"value\":", static_cast<unsigned long long>(offset));
            write_json_string(log, value);
            std::fputc('}', log);
            comma = true;
        }
        std::fputs("],\"vectors\":[", log);
        comma = false;
        for (size_t offset = 0; offset + 24 <= 0x798; offset += 8) {
            const auto begin = *reinterpret_cast<const uintptr_t*>(object + offset);
            const auto end = *reinterpret_cast<const uintptr_t*>(object + offset + 8);
            const auto capacity = *reinterpret_cast<const uintptr_t*>(object + offset + 16);
            if (begin < 0x10000 || end <= begin || end > capacity || end - begin > 256 ||
                (end - begin) % 32 || capacity - begin > 1024 * 1024) continue;
            char member[128]{};
            if (!copy_small_string(reinterpret_cast<const unsigned char*>(begin), 0, member, sizeof(member)) ||
                std::strncmp(member, "wxid_", 5)) continue;
            if (comma) std::fputc(',', log);
            std::fprintf(log, "{\"offset\":%llu,\"member\":", static_cast<unsigned long long>(offset));
            write_json_string(log, member);
            std::fputc('}', log);
            comma = true;
        }
        std::fputs("],\"object_hex\":\"", log);
        for (size_t offset = 0; offset < 0x798; ++offset) std::fprintf(log, "%02x", object[offset]);
        std::fputs("\"}\n", log);
        std::fclose(log);
    } __except (EXCEPTION_EXECUTE_HANDLER) { }
}

static void* __fastcall mention_probe_send(void* manager, void* output, SharedMessage* message, uint8_t flag) {
    _InterlockedIncrement(&active_callbacks);
    void* result = nullptr;
    __try {
        capture_mention_source(message);
        result = original_send(manager, output, message, flag);
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxArmMentionProbe(void* parameter) {
    if (!parameter || hook_created || receive_hook_created) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId()) return 3;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    mention_probe_config = config;
    triggered_once = 0;
    MH_STATUS status = MH_Initialize();
    if (status != MH_OK) return 600 + status;
    void* target = reinterpret_cast<void*>(core_base + 0x178f100);
    status = MH_CreateHook(target, reinterpret_cast<void*>(&mention_probe_send), reinterpret_cast<void**>(&original_send));
    if (status != MH_OK) { MH_Uninitialize(); return 600 + status; }
    status = MH_EnableHook(target);
    if (status != MH_OK) { MH_RemoveHook(target); MH_Uninitialize(); return 600 + status; }
    hook_created = true;
    return 0;
}
