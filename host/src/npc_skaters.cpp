#include "npc_skaters.h"
#include "board_native.h"
#include "game_probe.h"
#include "natives.h"
#include "pose_hook.h"

#include <windows.h>
#include <cmath>
#include <cstdio>
#include <vector>

namespace npcskaters {
namespace {

using gta::Call;

struct Slot {
    int ped = 0;
    std::uint32_t model = 0;
    std::uint32_t generation = 0;
    std::uint32_t requested = 0;  // model being loaded
    std::uintptr_t skeleton = 0;
    int bones = 0;
    int resolveIn = 0;            // frames until the skeleton is re-read
    bool abandoned = false;       // died: left to GTA until the next respawn
};

void (*g_log)(const char*) = nullptr;
void* g_runtime = nullptr;
SvNpcConfigureFn g_configure = nullptr;
SvNpcFrameFn g_frame = nullptr;
SvNpcGetFn g_get = nullptr;
int g_count = 0;
Slot g_slots[posehook::kMaxNpcs];
std::vector<float> g_bones(512 * 16);
bool g_any = false;

void Log(const char* s) {
    if (g_log) g_log(s);
}

void Release(int i, bool keepPed) {
    Slot& s = g_slots[i];
    posehook::UninstallNpc(i, Log);
    if (s.ped && Call<BOOL>(gta::DOES_ENTITY_EXIST, s.ped)) {
        if (keepPed) {
            Call<void>(gta::FREEZE_ENTITY_POSITION, s.ped, 0);
            Call<void>(gta::SET_PED_AS_NO_LONGER_NEEDED, &s.ped);
        } else {
            Call<void>(gta::DELETE_PED, &s.ped);
        }
    }
    s.ped = 0;
    s.skeleton = 0;
    s.bones = 0;
    s.model = 0;
}

void Hide(int i) {
    if (g_slots[i].ped) Release(i, false);
    boardnative::TickNpc(i, false, SvBoardPose{});
}

// A ped of `model` at `p`, kept still and out of GTA's own AI; the pose hook
// draws it skating.
int Create(std::uint32_t model, const SvVec3& p, float heading) {
    const int ped = Call<int>(gta::CREATE_PED, 4, model, p.x, p.y, p.z + 1.0f, heading, false, false);
    if (!ped) return 0;
    Call<void>(gta::SET_ENTITY_AS_MISSION_ENTITY, ped, true, true);
    Call<void>(gta::SET_PED_RANDOM_COMPONENT_VARIATION, ped, 0);
    Call<void>(gta::SET_BLOCKING_OF_NON_TEMPORARY_EVENTS, ped, true);
    Call<void>(gta::SET_PED_CAN_RAGDOLL, ped, false);
    Call<void>(gta::SET_PED_CAN_BE_TARGETTED, ped, false);
    Call<void>(gta::FREEZE_ENTITY_POSITION, ped, true);
    Call<void>(gta::SET_ENTITY_COLLISION, ped, false, false);
    return ped;
}

void Update(int i, const SvNpc& n, const float* bones) {
    Slot& s = g_slots[i];
    if (n.generation != s.generation) {
        s.generation = n.generation;
        s.abandoned = false;
        if (s.ped && s.model != n.model_hash) Release(i, false);
    }
    if (s.abandoned) {
        boardnative::TickNpc(i, false, SvBoardPose{});
        return;
    }
    if (s.ped && !Call<BOOL>(gta::DOES_ENTITY_EXIST, s.ped)) {
        posehook::UninstallNpc(i, Log);
        s.ped = 0;
    }
    if (!s.ped) {
        if (!Call<BOOL>(gta::IS_MODEL_IN_CDIMAGE, n.model_hash) || !Call<BOOL>(gta::IS_MODEL_A_PED, n.model_hash)) return;
        if (!Call<BOOL>(gta::HAS_MODEL_LOADED, n.model_hash)) {
            if (s.requested != n.model_hash) s.requested = n.model_hash;
            Call<void>(gta::REQUEST_MODEL, n.model_hash);
            return;
        }
        s.ped = Create(n.model_hash, n.position, n.heading_degrees);
        Call<void>(gta::SET_MODEL_AS_NO_LONGER_NEEDED, n.model_hash);
        s.requested = 0;
        if (!s.ped) return;
        s.model = n.model_hash;
        s.resolveIn = 0;
        char line[160];
        std::snprintf(line, sizeof(line), "npc skaters: skater %d is model %#010x", i + 1, n.model_hash);
        Log(line);
    }
    if (Call<BOOL>(gta::IS_PED_DEAD_OR_DYING, s.ped, true)) {
        // Shot or run over: GTA takes the body; the skater rides again at its next respawn.
        Release(i, true);
        s.abandoned = true;
        boardnative::TickNpc(i, false, SvBoardPose{});
        return;
    }
    Call<void>(gta::SET_ENTITY_COORDS_NO_OFFSET, s.ped, n.position.x, n.position.y, n.position.z + 1.0f, 1, 1, 0);
    if (std::isfinite(n.heading_degrees)) Call<void>(gta::SET_ENTITY_HEADING, s.ped, n.heading_degrees);
    if (--s.resolveIn <= 0) {
        s.resolveIn = 60;
        probe::SkeletonInfo sk;
        const char* why = "";
        if (probe::ResolvePedSkeleton(s.ped, sk, why) &&
            posehook::InstallNpc(i, reinterpret_cast<std::uintptr_t>(getScriptHandleBaseAddress(s.ped)), sk, Log)) {
            s.skeleton = sk.skeleton;
            s.bones = sk.count;
        } else {
            s.bones = 0;
        }
    }
    if (s.bones > 0 && static_cast<int>(n.bone_count) == s.bones) posehook::PublishNpcPose(i, bones, s.bones);
    boardnative::TickNpc(i, true, n.board);
}

} // namespace

void Start(void* module, void* runtime, const std::wstring& iniPath, const std::string& pedCache, void (*log)(const char*)) {
    g_log = log;
    g_runtime = runtime;
    g_count = static_cast<int>(GetPrivateProfileIntW(L"SkateV", L"NpcSkaters", 3, iniPath.c_str()));
    if (g_count < 0) g_count = 0;
    if (g_count > posehook::kMaxNpcs) g_count = posehook::kMaxNpcs;
    auto* m = static_cast<HMODULE>(module);
    g_configure = m ? reinterpret_cast<SvNpcConfigureFn>(GetProcAddress(m, "sv_npc_configure")) : nullptr;
    g_frame = m ? reinterpret_cast<SvNpcFrameFn>(GetProcAddress(m, "sv_npc_frame")) : nullptr;
    g_get = m ? reinterpret_cast<SvNpcGetFn>(GetProcAddress(m, "sv_npc_get")) : nullptr;
    char line[200];
    if (!g_configure || !g_frame || !g_get || !runtime) {
        Log("npc skaters: this runtime has no NPC skaters");
        g_count = 0;
        return;
    }
    if (pedCache.empty()) {
        Log("npc skaters: no ped cache (PedCache); off");
        g_count = 0;
        return;
    }
    g_configure(runtime, static_cast<std::uint32_t>(g_count), pedCache.c_str());
    std::snprintf(line, sizeof(line), "npc skaters: %d (NpcSkaters)", g_count);
    Log(line);
}

void Tick(bool skating, const SvVec3& player, float dt) {
    if (!g_count || !g_frame) return;
    g_frame(g_runtime, dt, &player, skating ? 1u : 0u);
    if (!skating) {
        if (g_any) Stop();
        return;
    }
    g_any = true;
    for (int i = 0; i < g_count; ++i) {
        SvNpc n{};
        n.size = sizeof(SvNpc);
        if (!g_get(g_runtime, static_cast<std::uint32_t>(i), &n, g_bones.data(), 512) || !(n.flags & 1)) {
            Hide(i);
            g_slots[i].generation = n.generation;
            continue;
        }
        Update(i, n, g_bones.data());
    }
}

void Stop() {
    for (int i = 0; i < posehook::kMaxNpcs; ++i) {
        if (g_slots[i].ped) Release(i, false);
        g_slots[i].abandoned = false;
    }
    boardnative::StopNpcs();
    g_any = false;
}

} // namespace npcskaters
