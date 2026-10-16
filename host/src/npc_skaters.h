#pragma once
#include <string>
#include "skatev_runtime.h"

// NPC skaters: ambient GTA peds riding their own Skate sessions near the
// player (runtime npc_pool.rs, sv_npc_*). The host spawns a ped of the
// model the runtime picked, places it where Skate's skater is, writes the
// pose through the pose hook's NPC table and shows a native board. Ini
// [SkateV] NpcSkaters=0..4 (default 3). Script thread only.
namespace npcskaters {

// After the runtime loaded. `module`/`runtime`: SkateVRuntime.dll and its
// handle (exports resolved by name; an older runtime leaves this off).
void Start(void* module, void* runtime, const std::wstring& iniPath, const std::string& pedCache, void (*log)(const char*));
// Every script frame: `skating` while the player rides (NPCs ride only then).
void Tick(bool skating, const SvVec3& player, float dt);
// Delete every NPC ped and board (leaving skate mode is handled by Tick).
void Stop();

} // namespace npcskaters
