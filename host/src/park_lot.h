#pragma once
// Skate 3's Create-a-Park lots: Team HQ and Hartley Stadium, Skate 3's own
// skate.Park lots built from the player's disc (tools/prepare-park-lots.py),
// far out over the sea; <DataRoot>/private/parks/parks.txt says where and
// which pieces. Inside, the Object Dropper (LB + B, Skate 3's "Edit park")
// builds the park.
//
// Like Skate 3's "Enter / Leave Team HQ «Park" teleports: two markers outside
// LSIA's big hangar door (a map blip finds them) warp in, an exit marker in
// each lot warps back out by the door; E / d-pad right in a marker. The SK8V
// menu's skate.Park page does the same and saves / loads the park.
// The lots are loaded the Object Dropper's way, not as a GTA map (a map in
// SK8V's startup pack crashed Story Mode or never streamed, 2026-10-15): the
// pieces' .ydr (embedded collision for GTA) and the .ytyp are registered
// while the game runs, and entering spawns every piece frozen at the lot.
// Skate gets them as placed objects (Objects(); their exact templates, the
// world cache's .prop-models), built into its static area with their rails.
// While skating, main.cpp's teleport path (ped moved by something else)
// leaves Skate, waits for the floor and gets back on the board.
//
// Save / Load keep the dropped objects per lot in %LOCALAPPDATA%\SkateV\parks
// (model hash, position, rotation, one object a line); Load puts them back
// through the dropper (SetRestore, dropper::Restore).
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace parklot {

// One dropped object: model hash, GTA world position of its origin and
// rotation in degrees (GET_ENTITY_ROTATION order 2).
struct Placed {
    std::uint32_t model = 0;
    float pos[3]{};
    float rot[3]{};
};

// Save file text, and back (false: not a park file). Pure, no GTA.
std::string Serialize(const std::vector<Placed>& objects);
bool Parse(const std::string& text, std::vector<Placed>& out);

// One lot as setup wrote it (parks.txt).
struct Lot {
    std::string key, name;
    float spawn[3]{}, heading = 0.0f, exit[3]{}, origin[3]{}, reach = 0.0f;
    std::vector<std::string> pieces;  // model names, spawned at `origin`
};
// parks.txt's text -> lots (rows that do not read are skipped). Pure, no GTA.
std::vector<Lot> ParseLots(const std::string& text);

using LogFn = void (*)(const char*);
// Once: the log, on-screen notices, the folder park saves go in, and
// <DataRoot>/private/parks/parks.txt (missing: no lots, say rerun setup).
void Start(LogFn log, std::function<void(const std::string&)> notify, const std::wstring& saveDir,
           const std::wstring& lotsFile);
// The dropper's objects in the world (entity handles), for Save.
void SetObjects(std::function<std::vector<int>()> objects);
// Puts one saved object back as a dropped object (true when placed); unset:
// Load reports that the dropper cannot restore yet.
void SetRestore(std::function<bool(const Placed&)> restore);

// Menu actions (script thread). Lot 0 Team HQ, 1 Hartley Stadium.
void Enter(int lot);
void Exit();
void Save();
void Load();
// The player is in a lot (entered and not gone elsewhere since).
bool Inside();
// A warp is running (screen fading or the lot streaming in).
bool Busy();
// The current lot's spawned pieces (Skate builds them in like dropped objects).
const std::vector<int>& Objects();
// Script thread, once a frame. `skating`: Skate holds the player (its
// teleport path re-enters at the new place; otherwise the ped is frozen
// until the floor's collision is in). `menus`: an SK8V menu or the dropper
// has the pad (markers ignore the button).
void Tick(bool skating, bool menus);

}  // namespace parklot
