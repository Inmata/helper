// 本地图片发送适配自 Prometheus-first/helper 的 image_send.h。
struct ImagePath {
    int32_t references = -1;
    int32_t length = 0;
    uint32_t allocation = 0, padding = 0;
    int64_t offset = 24;
    wchar_t characters[1024]{};
};
static_assert(offsetof(ImagePath, characters) == 24, "Unexpected Qt array layout");

static void release_image_object(SharedMessage& message, TestResult* report) {
    __try { release_shared(message); }
    __except (record_exception(GetExceptionInformation(), report)) {}
}

static void release_image_string(SmallString* text, TestResult* report) {
    __try {
        if (text->capacity > 15 && text->capacity < 1024 * 1024) {
            void* allocation = *reinterpret_cast<void**>(text->data);
            size_t size = static_cast<size_t>(text->capacity + 1);
            if (size >= 4096) {
                allocation = *(reinterpret_cast<void**>(allocation) - 1);
                size += 39;
            }
            using Free = void (__fastcall*)(void*, size_t);
            reinterpret_cast<Free>(core_address(core_base, 0x74ab8e0))(allocation, size);
        }
    } __except (record_exception(GetExceptionInformation(), report)) {}
}

static void send_image_on_task(const TextConfig& config) {
    SharedMessage app{}, context{}, manager{}, message{};
    SmallString roundtrip{};
    roundtrip.capacity = 15;
    alignas(16) unsigned char result[128]{};
    ImagePath path{};
    TestResult report{};
    bool success = false;
    __try {
        path.length = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.base.text, -1,
                                          path.characters, 1024) - 1;
        if (path.length <= 0) RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
        ImagePath* qstring = &path;
        using ToUtf8 = SmallString* (__fastcall*)(void*, SmallString*);
        reinterpret_cast<ToUtf8>(core_address(core_base, 0x857a0))(&qstring, &roundtrip);
        const char* bytes = roundtrip.capacity > 15 ?
            *reinterpret_cast<const char* const*>(roundtrip.data) : roundtrip.data;
        if (roundtrip.length != std::strlen(config.base.text) ||
            std::memcmp(bytes, config.base.text, roundtrip.length))
            RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
        using GetApp = SharedMessage* (__fastcall*)(SharedMessage*);
        using GetShared = SharedMessage* (__fastcall*)(void*, SharedMessage*);
        reinterpret_cast<GetApp>(core_address(core_base, 0x47220))(&app);
        if (!app.object) RaiseException(ERROR_NOT_READY, 0, 0, nullptr);
        reinterpret_cast<GetShared>(core_address(core_base, 0x36c740))(app.object, &context);
        if (!context.object) RaiseException(ERROR_NOT_READY, 0, 0, nullptr);
        reinterpret_cast<GetShared>(core_address(core_base, 0x6f4d90))(context.object, &manager);
        if (!manager.object) RaiseException(ERROR_NOT_READY, 0, 0, nullptr);
        using ImageFromFile = SharedMessage* (__fastcall*)(void*, SharedMessage*, void*);
        reinterpret_cast<ImageFromFile>(core_address(core_base, 0x3d9d990))(nullptr, &message, &qstring);
        if (!message.object || !message.control) RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
        auto object = static_cast<unsigned char*>(message.object);
        struct WideStringView { wchar_t data[8]; uint64_t length, capacity; };
        const auto prepared = reinterpret_cast<const WideStringView*>(object + 0x120);
        // 微信生成上传暂存路径，发送前需把原图复制到该路径。
        if (!prepared->length || prepared->length >= 1024 || prepared->capacity < prepared->length)
            RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
        const wchar_t* destination = prepared->capacity > 7 ?
            *reinterpret_cast<const wchar_t* const*>(prepared->data) : prepared->data;
        if (!CopyFileW(path.characters, destination, TRUE)) RaiseException(GetLastError(), 0, 0, nullptr);
        if (*reinterpret_cast<uint32_t*>(object + 0x118) != 3 ||
            !set_native_string(reinterpret_cast<SmallString*>(object + 0xb0), config.target))
            RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
        SharedMessage transferred = message;
        _InterlockedIncrement(reinterpret_cast<volatile LONG*>(
            static_cast<unsigned char*>(message.control) + 8));
        reinterpret_cast<SendFunction>(core_address(core_base, 0x178f100))(manager.object, result, &transferred, 1);
        report.native_status = *reinterpret_cast<uint32_t*>(result);
        report.native_status_high = *reinterpret_cast<uint32_t*>(result + 4);
        report.stage = 21;
        success = report.native_status == 0 && report.native_status_high == 0;
    } __except (record_exception(GetExceptionInformation(), &report)) {}
    release_image_object(*reinterpret_cast<SharedMessage*>(result + 40), &report);
    release_image_string(reinterpret_cast<SmallString*>(result + 8), &report);
    release_image_string(&roundtrip, &report);
    release_image_object(message, &report);
    release_image_object(manager, &report);
    release_image_object(context, &report);
    release_image_object(app, &report);
    FILE* log = nullptr;
    if (_wfopen_s(&log, config.base.log_path, L"wb") == 0 && log) {
        std::fprintf(log, "{\"stage\":%lu,\"exception\":%lu,\"native_status\":%u,\"target\":",
                     report.stage, report.exception, report.native_status);
        write_json_string(log, config.target);
        std::fputs(",\"text\":", log);
        write_json_string(log, config.base.text);
        std::fprintf(log, ",\"completion_success\":%s}\n", success && !report.exception ? "true" : "false");
        std::fclose(log);
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxSendImage(void* parameter) {
    if (!parameter || live_tasks || live_completions) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId() ||
        !valid_target(config.target) || !strnlen_s(config.base.text, sizeof(config.base.text)) ||
        strnlen_s(config.base.text, sizeof(config.base.text)) == sizeof(config.base.text))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    auto lease = std::make_shared<TextTaskLease>(config);
    std::function<void()> task = [lease]() {
        _InterlockedIncrement(&active_callbacks);
        __try { send_image_on_task(lease->config); }
        __finally { _InterlockedDecrement(&active_callbacks); }
    };
    return schedule_task(config.base, &task);
}
