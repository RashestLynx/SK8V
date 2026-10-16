#pragma once
// Skate 3's Object Dropper on the session-marker chord, as Skate 3 plays it:
// hold LB and press B for the catalogue (LT/RT tab, up/down and A to pick a
// group, then an object; B back), then place it: a ghost of the object that
// the left stick moves (camera relative), the right stick (or RT + left
// stick) turns and the d-pad raises; A places it, LB toggles Duplicate (A
// then keeps a copy to place again), LT held fine-tunes, Y deletes the
// nearest dropped object, L3 resets the ghost, X picks a new object, B goes
// back. Skate holds still while the dropper is open.
// The screen is Skate 3's own (`objectdropper` movie, drawn by the runtime
// HUD through sv_set_object_dropper); without it the host draws a text list.
// The objects are Skate 3's own (setup builds them from the player's disc:
// tools/prepare-dropper.py) and GTA's skate-park props, frozen; their exact
// templates (the world cache's .prop-models) give Skate their collision, and
// the runtime builds them into the static area so their lips grind
// (SvDynamicBody flag 2).
#include "skatev_runtime.h"
#include <cstdint>
#include <string>
#include <vector>

namespace dropper {

// Most objects out at once; a drop past it removes the oldest.
constexpr std::size_t kMaxObjects = 48;

// With the pad about to go to Skate (gun away). LB + B opens; while open the
// dropper takes the whole pad. `pressed`: buttons down this frame and not the last.
void Controls(SvPad& pad, std::uint16_t pressed);
// Once a frame while skating: registers Skate 3's objects once streaming is
// up, moves the ghost, spawns what was placed or restored, and sends the
// screen's state to the runtime. `skater`, `heading`: GTA position and heading.
void Tick(SvVec3 skater, float heading);
// The host's own list, only when the runtime has no Skate 3 screen.
void Draw();
void SetLog(void (*log)(const char*));
// Where setup put Skate 3's objects: <DataRoot>/private/dropper.
void SetDataRoot(const std::wstring& dataRoot);
// The runtime's Skate 3 screen (sv_set_object_dropper); returns whether it shows.
void SetPresenter(bool (*present)(const SvObjectDropper&));
bool IsOpen();
void Close();
// The placement ghost (0: none): not collision, for Skate or GTA.
int Ghost();
// The dropped objects still in the world (DynamicWorld flags them).
const std::vector<int>& Objects();
// Drops `model` at `pos` with GTA rotation `rotDeg` (order 2), as placed
// (a saved park): queued until its model streams in. False if GTA lacks it.
bool Restore(std::uint32_t model, SvVec3 pos, SvVec3 rotDeg);
// Deletes every dropped object (Skate mode ended).
void Clear();

} // namespace dropper
