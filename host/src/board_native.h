#pragma once
#include <string>
#include "skatev_runtime.h"
namespace boardnative {
// Call on the ScriptHookV script thread, after the Legacy Story Mode guard.
bool Start(const std::wstring& iniPath,const std::wstring& dataRoot,void (*log)(const char*));
// True only when the native custom object exists and all seven bones were posed.
bool Tick(bool skating,const SvBoardPose& pose);
// Put away only: delete presentation on the script thread, retain the
// registered resources and loaded archetype/model for the next invocation.
void Stop();
int Entity(); // Exclude this presentation-only entity from ExternalQueries.
// NPC skaters' boards (npc_skaters.cpp), script thread: slot `slot`'s board
// object shown at `pose` (hidden when !show). Needs the player's board model
// loaded (Start + a Tick). StopNpcs deletes them all.
bool TickNpc(int slot,bool show,const SvBoardPose& pose);
void StopNpcs();
bool IsNpcBoard(int entity);
}
