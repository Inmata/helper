#pragma once

// 4.2.0.12 / 4.2.0.13: AddMsg 的服务器编号在入库后的 DTO 仍为 +0x148。
static constexpr uintptr_t voice_commit_rva = 0x17f7050;
static constexpr uintptr_t voice_task_rva = 0x5d25550;
using VoiceCommit = void (__fastcall*)(void*, const uintptr_t*, uint8_t);
using VoiceTask = void (__fastcall*)(const unsigned char*);
static VoiceCommit original_voice_commit = nullptr;
static VoiceTask original_voice_task = nullptr;
static SRWLOCK voice_lock = SRWLOCK_INIT;
static char voice_rooms[1024]{};

struct PendingVoice {
    uint64_t id, started;
    bool submitted;
    char sender[128], receiver[128], text[8192], source[4096];
};
static PendingVoice pending_voices[32]{};
static size_t next_voice = 0;

static bool remember_voice(uint64_t id, const char* sender, const char* receiver,
                           const char* text, const char* source) {
    if (!id || !voice_rooms[0]) return false;
    const size_t length = std::strlen(sender);
    for (const char* room = voice_rooms; *room;) {
        const size_t count = std::strcspn(room, "\n");
        if (count == length && !std::memcmp(room, sender, length)) {
            AcquireSRWLockExclusive(&voice_lock);
            for (const auto& voice : pending_voices) {
                if (voice.id == id) {
                    ReleaseSRWLockExclusive(&voice_lock);
                    return true;
                }
            }
            auto& voice = pending_voices[next_voice++ % std::size(pending_voices)];
            voice.id = id;
            voice.started = GetTickCount64();
            voice.submitted = false;
            strcpy_s(voice.sender, sender);
            strcpy_s(voice.receiver, receiver);
            strcpy_s(voice.text, text);
            strcpy_s(voice.source, source);
            ReleaseSRWLockExclusive(&voice_lock);
            return true;
        }
        room += count + (room[count] == '\n');
    }
    return false;
}

static void finish_voice(uint64_t id, const char* audio, uint32_t size) {
    PendingVoice voice{};
    AcquireSRWLockExclusive(&voice_lock);
    for (auto& pending : pending_voices) {
        if (pending.id == id && pending.submitted) {
            voice = pending;
            pending.id = 0;
            break;
        }
    }
    ReleaseSRWLockExclusive(&voice_lock);
    if (voice.id)
        enqueue_received(voice.sender, voice.receiver, voice.text, voice.source, 34, nullptr,
                         id, audio, size, 1 + static_cast<uint32_t>(GetTickCount64() - voice.started));
}

static void __fastcall voice_task(const unsigned char* context) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        original_voice_task(context);
        const auto message = *reinterpret_cast<const unsigned char* const*>(context + 8);
        const auto id = *reinterpret_cast<const uint64_t*>(message + 0x148);
        const auto buffer = *reinterpret_cast<const unsigned char* const*>(context + 0x18);
        const auto length = *reinterpret_cast<const uint64_t*>(buffer + 0x10);
        const auto expected = *reinterpret_cast<const uint32_t*>(context + 0x3c);
        const auto status = *reinterpret_cast<const int32_t*>(context + 0x48);
        const bool complete = status == 0 && length == expected && length <= sizeof(ReceivedMediaMessageV5::payload);
        finish_voice(id, complete ? *reinterpret_cast<const char* const*>(buffer) : nullptr,
                     complete ? static_cast<uint32_t>(length) : 0);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

struct VoiceRequest {
    SharedMessage message{};
    void* manager;
    uint64_t id;
    VoiceRequest(void* owner, uint64_t number) : manager(owner), id(number) { _InterlockedIncrement(&live_tasks); }
    ~VoiceRequest() { release_shared(message); _InterlockedDecrement(&live_tasks); }
};

static void request_voice(const VoiceRequest* request) {
    SharedMessage app{}, center{}, manager{};
    // 该任务的返回值只确认提交；完整音频由 voice_task 回填原消息。
    alignas(16) unsigned char result[0x38]{};
    TestResult error{};
    __try {
        using App = void (__fastcall*)(SharedMessage*);
        using Service = void (__fastcall*)(void*, SharedMessage*);
        reinterpret_cast<App>(core_address(core_base, 0x47220))(&app);
        if (app.object) reinterpret_cast<Service>(core_address(core_base, 0x36c740))(app.object, &center);
        if (center.object) reinterpret_cast<Service>(core_address(core_base, 0x6f4d90))(center.object, &manager);
        if (manager.object != request->manager) {
            finish_voice(request->id, nullptr, 0);
        } else {
            using Download = void (__fastcall*)(void*, void*, const SharedMessage*, int);
            reinterpret_cast<Download>(core_address(core_base, 0x1844640))(manager.object, result, &request->message, 0);
            const auto status = *reinterpret_cast<const int32_t*>(result + 4);
            reinterpret_cast<void (__fastcall*)(void*)>(core_address(core_base, 0x5a590))(result);
            if (status) finish_voice(request->id, nullptr, 0);
        }
    } __except (record_exception(GetExceptionInformation(), &error)) {
        finish_voice(request->id, nullptr, 0);
    }
    release_shared(manager);
    release_shared(center);
    release_shared(app);
}

static void queue_voice_downloads(void* manager, const uintptr_t* messages) {
    for (auto address = messages[0]; address < messages[1]; address += 0x2e8) {
        if (*reinterpret_cast<const uint32_t*>(address + 0xc) != 34) continue;
        const auto id = *reinterpret_cast<const uint64_t*>(address + 0x148);
        if (!id) continue;
        bool matched = false;
        AcquireSRWLockExclusive(&voice_lock);
        for (auto& voice : pending_voices) {
            if (voice.id == id && !voice.submitted) {
                voice.submitted = matched = true;
                break;
            }
        }
        ReleaseSRWLockExclusive(&voice_lock);
        if (!matched) continue;
        auto request = std::make_shared<VoiceRequest>(manager, id);
        using Copy = void (__fastcall*)(const void*, SharedMessage*);
        reinterpret_cast<Copy>(core_address(core_base, 0xaa1870))(reinterpret_cast<const void*>(address), &request->message);
        std::function<void()> task = [request]() { request_voice(request.get()); };
        if (schedule_task(TestConfig{}, &task)) finish_voice(id, nullptr, 0);
    }
}

static void __fastcall voice_commit(void* manager, const uintptr_t* messages, uint8_t flag) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        original_voice_commit(manager, messages, flag);
        queue_voice_downloads(manager, messages);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static MH_STATUS install_voice_download(const char* rooms) {
    strcpy_s(voice_rooms, rooms);
    std::memset(pending_voices, 0, sizeof(pending_voices));
    next_voice = 0;
    auto status = MH_CreateHook(reinterpret_cast<void*>(core_address(core_base, voice_commit_rva)),
                                reinterpret_cast<void*>(&voice_commit), reinterpret_cast<void**>(&original_voice_commit));
    if (status == MH_OK)
        status = MH_CreateHook(reinterpret_cast<void*>(core_address(core_base, voice_task_rva)),
                               reinterpret_cast<void*>(&voice_task), reinterpret_cast<void**>(&original_voice_task));
    return status;
}
