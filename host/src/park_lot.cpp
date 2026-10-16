// Skate 3's Create-a-Park lots (see park_lot.h).
#include "park_lot.h"
#include <windows.h>
#include <main.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sstream>
#include "host_util.h"
#include "natives.h"
#include "streaming.h"

namespace parklot {

std::string Serialize(const std::vector<Placed>& objects) {
    std::string out = "SK8VPARK 1\n";
    char line[160];
    for (const Placed& p : objects) {
        std::snprintf(line, sizeof(line), "%08X %.3f %.3f %.3f %.2f %.2f %.2f\n", p.model, p.pos[0], p.pos[1], p.pos[2],
                      p.rot[0], p.rot[1], p.rot[2]);
        out += line;
    }
    return out;
}

bool Parse(const std::string& text, std::vector<Placed>& out) {
    out.clear();
    std::istringstream in(text);
    std::string line;
    if (!std::getline(in, line) || line.rfind("SK8VPARK 1", 0) != 0) return false;
    while (std::getline(in, line)) {
        if (line.empty() || line[0] == '#') continue;
        std::istringstream f(line);
        std::string model;
        Placed p;
        if (!(f >> model >> p.pos[0] >> p.pos[1] >> p.pos[2] >> p.rot[0] >> p.rot[1] >> p.rot[2])) return false;
        char* end = nullptr;
        p.model = static_cast<std::uint32_t>(std::strtoul(model.c_str(), &end, 16));
        if (!end || *end || !p.model) return false;
        out.push_back(p);
    }
    return true;
}

bool ParseLayout(const std::string& text, std::vector<LayoutPiece>& out) {
    out.clear();
    std::istringstream in(text);
    std::string line;
    if (!std::getline(in, line) || line.rfind("SK8VLAYOUT 1", 0) != 0) return false;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line[0] == '#') continue;
        std::istringstream f(line);
        std::string model;
        LayoutPiece p;
        if (!(f >> model >> p.pos[0] >> p.pos[1] >> p.pos[2] >> p.quat[0] >> p.quat[1] >> p.quat[2] >> p.quat[3])) return false;
        char* end = nullptr;
        p.model = static_cast<std::uint32_t>(std::strtoul(model.c_str(), &end, 16));
        if (!end || *end || !p.model) return false;
        out.push_back(p);
    }
    return true;
}

std::vector<Lot> ParseLots(const std::string& text) {
    std::vector<Lot> lots;
    std::istringstream in(text);
    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line[0] == '#') continue;
        std::vector<std::string> cols;
        std::istringstream row(line);
        for (std::string col; std::getline(row, col, '\t');) cols.push_back(col);
        if (cols.size() != 8) continue;
        Lot lot;
        lot.key = cols[0], lot.name = cols[1];
        const auto three = [](const std::string& s, float* v) { return sscanf_s(s.c_str(), "%f %f %f", v, v + 1, v + 2) == 3; };
        if (!three(cols[2], lot.spawn) || sscanf_s(cols[3].c_str(), "%f", &lot.heading) != 1 || !three(cols[4], lot.exit) ||
            !three(cols[5], lot.origin) || sscanf_s(cols[6].c_str(), "%f", &lot.reach) != 1 || lot.key.empty())
            continue;
        std::istringstream pieces(cols[7]);
        for (std::string piece; std::getline(pieces, piece, ',');)
            if (!piece.empty()) lot.pieces.push_back(piece);
        if (lot.pieces.empty()) continue;
        lots.push_back(lot);
    }
    return lots;
}

namespace {
using gta::Call;

// The entrance: outside LSIA's big hangar door (v_hanger's 78 m portal to the
// outside, centred at (-1011.65, -2983.14, 12.73); outward (-0.976, 0.215)).
// One marker per lot, 8 m apart along the door, 12 m out; leaving puts the
// player 18 m out, facing away from the hangar.
constexpr float kDoor[2] = {-1011.65f, -2983.14f}, kOut[2] = {-0.976f, 0.215f}, kAlong[2] = {0.215f, 0.976f};
constexpr float kDoorZ = 13.5f;  // until GTA's ground there is known
constexpr float kMarkerRadius = 1.6f, kMarkerShow = 120.0f;
constexpr int kContext = 51;  // INPUT_CONTEXT: E, d-pad right
constexpr int kFadeMs = 500;
constexpr DWORD kFadeTimeout = 3000, kSettleTimeout = 15000, kStreamMs = 1500;
// Below the lot's floor by this much: fell out of it (back to its spawn, at
// most kFalls times in a row; then out by the door: the lot did not load).
constexpr float kFallBelow = 25.0f;
constexpr int kFalls = 3;
// The pieces' models stream in this long at most before the warp gives up.
constexpr DWORD kModelTimeout = 15000;

enum class Step { Idle, FadeOut, Settle };

LogFn g_log = nullptr;
std::function<void(const std::string&)> g_notify;
std::wstring g_saveDir;
std::function<std::vector<int>()> g_objects;
std::function<bool(const Placed&)> g_restore;
std::vector<Lot> g_lots;

Step g_step = Step::Idle;
DWORD g_stepSince = 0;
int g_lot = -1;      // the lot the player is in (-1: none)
int g_target = -1;   // the running warp's lot (-1: out by the door)
float g_to[3]{}, g_toHeading = 0.0f;
bool g_frozen = false;
float g_doorZ = kDoorZ;
bool g_doorGround = false;
int g_blip = 0;
int g_falls = 0;
// The lots' models: <DataRoot>/private/parks, registered once per process.
std::filesystem::path g_dir;
bool g_registered = false, g_registerFailed = false;
// The spawned pieces and their lot (-1: none); the lot's Skate 3 park (sorted handles).
std::vector<int> g_pieces, g_layout;
int g_piecesLot = -1;

void Log(const std::string& s) {
    if (g_log) g_log(("SkateV park: " + s).c_str());
}
void Say(const std::string& s) {
    Log(s);
    if (g_notify) g_notify(s);
}

std::wstring SavePath() { return g_saveDir + util::Wide(g_lots[static_cast<std::size_t>(g_lot)].key) + L".park"; }

void SetStep(Step step) {
    g_step = step;
    g_stepSince = GetTickCount();
}

// Lot i's entrance marker by the hangar door, on the ground.
void Entrance(std::size_t i, float out[3]) {
    const float side = (static_cast<float>(i) - (static_cast<float>(g_lots.size()) - 1) / 2) * 8.0f;
    out[0] = kDoor[0] + kOut[0] * 12 + kAlong[0] * side;
    out[1] = kDoor[1] + kOut[1] * 12 + kAlong[1] * side;
    out[2] = g_doorZ;
}

void Unfreeze(int ped) {
    if (g_frozen && Call<BOOL>(gta::DOES_ENTITY_EXIST, ped)) Call<void>(gta::FREEZE_ENTITY_POSITION, ped, 0);
    g_frozen = false;
}

// The lots' .ydr and .ytyp, registered like the dropper's objects (streaming.h).
void Register() {
    if (g_registered || g_registerFailed || g_dir.empty() || !streaming::Ready()) return;
    const auto ytyp = g_dir / L"sk8v_parks.ytyp";
    const bool present = std::filesystem::is_regular_file(ytyp);
    if (!present || !streaming::Guards()) {
        g_registerFailed = true;
        Log(present ? "3889 streaming function guards differ" : "no lot models (rerun SK8V setup)");
        return;
    }
    const auto reg = streaming::Registrar();
    int count = 0;
    for (const Lot& l : g_lots)
        for (const std::string& piece : l.pieces) {
            const auto ydr = g_dir / (piece + ".ydr");
            const std::string path = streaming::NarrowAnsi(ydr), name = piece + ".ydr";
            if (path.empty() || !std::filesystem::is_regular_file(ydr)) continue;
            std::uint32_t id = ~0u;
            reg(&id, path.c_str(), true, name.c_str(), false, false);
            if (id != ~0u) ++count;
        }
    const std::string path = streaming::NarrowAnsi(ytyp);
    std::uint32_t id = ~0u;
    if (!path.empty()) reg(&id, path.c_str(), true, "sk8v_parks.ytyp", false, false);
    if (id == ~0u) {
        g_registerFailed = true;
        Log("lot types did not register");
        return;
    }
    streaming::TypeLoader()("sk8v_parks.ytyp");
    g_registered = true;
    Log(std::to_string(count) + " lot models registered");
}

// The lot's models and its Skate 3 park's (the Object Dropper's, registered by it), each once.
std::vector<Hash> LotModels(int lot) {
    const Lot& l = g_lots[static_cast<std::size_t>(lot)];
    std::vector<Hash> out;
    for (const std::string& piece : l.pieces) out.push_back(util::Joaat(piece));
    for (const LayoutPiece& p : l.layout)
        if (std::find(out.begin(), out.end(), static_cast<Hash>(p.model)) == out.end()) out.push_back(static_cast<Hash>(p.model));
    return out;
}

bool PiecesLoaded(int lot) {
    bool all = true;
    for (const Hash h : LotModels(lot)) {
        if (!Call<BOOL>(gta::HAS_MODEL_LOADED, h)) {
            Call<void>(gta::REQUEST_MODEL, h);
            all = false;
        }
    }
    return all;
}

void DeletePieces() {
    g_pieces.insert(g_pieces.end(), g_layout.begin(), g_layout.end());
    g_layout.clear();
    for (int object : g_pieces) {
        if (!Call<BOOL>(gta::DOES_ENTITY_EXIST, object)) continue;
        Call<void>(gta::SET_ENTITY_AS_MISSION_ENTITY, object, 1, 1);
        Call<void>(gta::DELETE_OBJECT, &object);
    }
    if (g_piecesLot >= 0)
        for (const Hash h : LotModels(g_piecesLot)) Call<void>(gta::SET_MODEL_AS_NO_LONGER_NEEDED, h);
    g_pieces.clear();
    g_piecesLot = -1;
}

// Every piece of the lot, frozen at its origin (they share it), ours (mission entities).
void SpawnPieces(int lot) {
    DeletePieces();
    const Lot& l = g_lots[static_cast<std::size_t>(lot)];
    for (const std::string& piece : l.pieces) {
        const int object = Call<int>(gta::CREATE_OBJECT_NO_OFFSET, util::Joaat(piece), l.origin[0], l.origin[1], l.origin[2],
                                     false, false, false, 0);
        if (!object) {
            Log("piece " + piece + " did not spawn");
            continue;
        }
        Call<void>(gta::SET_ENTITY_AS_MISSION_ENTITY, object, 1, 1);
        Call<void>(gta::FREEZE_ENTITY_POSITION, object, 1);
        g_pieces.push_back(object);
    }
    // Skate 3's park in the lot: the Object Dropper's models where Skate 3 put them.
    for (const LayoutPiece& p : l.layout) {
        const int object = Call<int>(gta::CREATE_OBJECT_NO_OFFSET, static_cast<Hash>(p.model), p.pos[0], p.pos[1], p.pos[2],
                                     false, false, false, 0);
        if (!object) continue;
        Call<void>(gta::SET_ENTITY_QUATERNION, object, p.quat[0], p.quat[1], p.quat[2], p.quat[3]);
        Call<void>(gta::SET_ENTITY_AS_MISSION_ENTITY, object, 1, 1);
        Call<void>(gta::FREEZE_ENTITY_POSITION, object, 1);
        g_layout.push_back(object);
    }
    std::sort(g_layout.begin(), g_layout.end());
    g_piecesLot = lot;
    Log(l.name + ": " + std::to_string(g_pieces.size()) + " of " + std::to_string(l.pieces.size()) + " pieces spawned, " +
        std::to_string(g_layout.size()) + " of " + std::to_string(l.layout.size()) + " of its Skate 3 park");
}

bool CanWarp(int ped) {
    if (g_step != Step::Idle) return false;
    if (!Call<BOOL>(gta::DOES_ENTITY_EXIST, ped) || Call<BOOL>(gta::IS_ENTITY_DEAD, ped, 0)) return false;
    if (Call<BOOL>(gta::IS_PED_IN_ANY_VEHICLE, ped, 0)) {
        Say("Get out of the vehicle first");
        return false;
    }
    return true;
}

// Starts a warp: into lot `lot` (its spawn), or out by the door (-1).
void Warp(int lot) {
    g_target = lot;
    if (lot >= 0) {
        const Lot& l = g_lots[static_cast<std::size_t>(lot)];
        for (int k = 0; k < 3; ++k) g_to[k] = l.spawn[k];
        g_toHeading = l.heading;
    } else {
        g_to[0] = kDoor[0] + kOut[0] * 18, g_to[1] = kDoor[1] + kOut[1] * 18, g_to[2] = g_doorZ + 1.0f;
        g_toHeading = std::atan2(-kOut[0], kOut[1]) * 57.29578f;
    }
    Call<void>(gta::DO_SCREEN_FADE_OUT, kFadeMs);
    SetStep(Step::FadeOut);
}

// Moves the ped to the warp's target (freezing it there when Skate does not hold it).
void Place(int ped, bool skating) {
    Call<void>(gta::SET_FOCUS_POS_AND_VEL, g_to[0], g_to[1], g_to[2], 0.0f, 0.0f, 0.0f);
    Call<void>(gta::SET_ENTITY_COORDS_NO_OFFSET, ped, g_to[0], g_to[1], g_to[2], 0, 0, 0);
    Call<void>(gta::SET_ENTITY_HEADING, ped, g_toHeading);
    Call<void>(gta::SET_GAMEPLAY_CAM_RELATIVE_HEADING, 0.0f);
    Call<void>(gta::REQUEST_COLLISION_AT_COORD, g_to[0], g_to[1], g_to[2]);
    if (!skating) {
        Call<void>(gta::FREEZE_ENTITY_POSITION, ped, 1);
        g_frozen = true;
    }
    Log(std::string(g_target >= 0 ? "warped into " + g_lots[static_cast<std::size_t>(g_target)].name : "warped out by the hangar") +
        (skating ? " (Skate follows the teleport)" : ""));
}

void Finish(int ped, bool arrived) {
    Unfreeze(ped);
    Call<void>(gta::CLEAR_FOCUS);
    Call<void>(gta::DO_SCREEN_FADE_IN, kFadeMs);
    if (arrived) {
        g_lot = g_target;
        if (g_lot < 0) DeletePieces();
        Say(g_lot >= 0 ? g_lots[static_cast<std::size_t>(g_lot)].name +
                             " skate.Park: LB + B builds; the exit marker or the SK8V menu takes you back"
                       : "Left the skate.Park");
    }
    SetStep(Step::Idle);
}

bool InLot(const Lot& l, const Vector3& p) {
    const float dx = p.x - l.origin[0], dy = p.y - l.origin[1];
    return dx * dx + dy * dy < l.reach * l.reach && p.z > l.origin[2] - kFallBelow && p.z < l.origin[2] + 150.0f;
}

void Marker(const float at[3], int r, int g, int b) {
    Call<void>(gta::DRAW_MARKER, 1, at[0], at[1], at[2] - 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f, kMarkerRadius * 2,
               kMarkerRadius * 2, 1.2f, r, g, b, 140, 0, 0, 2, 0, static_cast<const char*>(nullptr),
               static_cast<const char*>(nullptr), 0);
}

bool Near(const Vector3& p, const float at[3]) {
    const float dx = p.x - at[0], dy = p.y - at[1], dz = p.z - at[2];
    return dx * dx + dy * dy < kMarkerRadius * kMarkerRadius && std::fabs(dz) < 3.0f;
}

void Prompt(const std::string& text) { gta::Text(text.c_str(), 0.5f, 0.80f, 0.42f, 255, 255, 255, 255, gta::Align::Centre); }

// The lot's name floating over its entrance marker (seen from kLabelShow metres; smaller farther off).
constexpr float kLabelShow = 45.0f;
void Label(const std::string& name, const float at[3], const Vector3& from) {
    const float dx = at[0] - from.x, dy = at[1] - from.y, dz = at[2] - from.z;
    const float d = std::sqrt(dx * dx + dy * dy + dz * dz);
    if (d > kLabelShow) return;
    float sx = 0.0f, sy = 0.0f;
    if (!Call<BOOL>(gta::GET_SCREEN_COORD_FROM_WORLD_COORD, at[0], at[1], at[2] + 1.6f, &sx, &sy)) return;
    const float scale = std::clamp(0.62f - d * 0.008f, 0.30f, 0.62f);
    gta::Text(name.c_str(), sx, sy, scale, 255, 220, 120, 255, gta::Align::Centre);
}

// The entrance blip on the map, once the lots are known.
void Blip() {
    if (g_lots.empty() || (g_blip && Call<BOOL>(gta::DOES_BLIP_EXIST, g_blip))) return;
    g_blip = Call<int>(gta::ADD_BLIP_FOR_COORD, kDoor[0] + kOut[0] * 12, kDoor[1] + kOut[1] * 12, kDoorZ);
    if (!g_blip) return;
    Call<void>(gta::SET_BLIP_SPRITE, g_blip, 1);
    Call<void>(gta::SET_BLIP_COLOUR, g_blip, 5);
    Call<void>(gta::SET_BLIP_AS_SHORT_RANGE, g_blip, 1);
    Call<void>(gta::BEGIN_TEXT_COMMAND_SET_BLIP_NAME, "STRING");
    Call<void>(gta::ADD_TEXT_COMPONENT_SUBSTRING_PLAYER_NAME, "skate.Park");
    Call<void>(gta::END_TEXT_COMMAND_SET_BLIP_NAME, g_blip);
}

// Markers, prompts and the button while nothing else runs.
void Markers(int ped, bool menus) {
    const Vector3 p = Call<Vector3>(gta::GET_ENTITY_COORDS, ped, 1);
    const bool pressed = !menus && Call<BOOL>(gta::IS_DISABLED_CONTROL_JUST_PRESSED, 0, kContext);
    if (g_lot >= 0) {
        const Lot& l = g_lots[static_cast<std::size_t>(g_lot)];
        Marker(l.exit, 255, 200, 0);
        if (Near(p, l.exit)) {
            Prompt("E / D-pad Right: leave " + l.name + " skate.Park");
            if (pressed && CanWarp(ped)) Warp(-1);
        }
        return;
    }
    const float dx = p.x - kDoor[0], dy = p.y - kDoor[1];
    if (dx * dx + dy * dy > kMarkerShow * kMarkerShow) return;
    if (!g_doorGround) {
        float ground = 0.0f;
        if (Call<BOOL>(gta::GET_GROUND_Z_FOR_3D_COORD, kDoor[0] + kOut[0] * 12, kDoor[1] + kOut[1] * 12, kDoorZ + 5.0f, &ground, 0, 0) &&
            std::isfinite(ground) && std::fabs(ground - kDoorZ) < 5.0f) {
            g_doorZ = ground, g_doorGround = true;
        }
    }
    for (std::size_t i = 0; i < g_lots.size(); ++i) {
        float at[3];
        Entrance(i, at);
        at[2] += 1.0f;
        Marker(at, 255, 200, 0);
        Label(g_lots[i].name, at, p);
        if (Near(p, at)) {
            Prompt("E / D-pad Right: enter " + g_lots[i].name + " skate.Park");
            if (pressed && CanWarp(ped)) Warp(static_cast<int>(i));
        }
    }
}
}  // namespace

void Start(LogFn log, std::function<void(const std::string&)> notify, const std::wstring& saveDir, const std::wstring& lotsFile) {
    g_log = log, g_notify = std::move(notify), g_saveDir = saveDir;
    if (!g_saveDir.empty() && g_saveDir.back() != L'\\') g_saveDir += L'\\';
    std::ifstream in(lotsFile, std::ios::binary);
    std::stringstream text;
    if (in) text << in.rdbuf();
    g_lots = ParseLots(text.str());
    g_dir = std::filesystem::path(lotsFile).parent_path();
    for (Lot& l : g_lots) {
        std::ifstream layout(g_dir / (util::Wide(l.key) + L".layout"), std::ios::binary);
        if (!layout) continue;
        std::stringstream lines;
        lines << layout.rdbuf();
        if (!ParseLayout(lines.str(), l.layout)) {
            l.layout.clear();
            Log(l.name + "'s layout does not read (rerun SK8V setup)");
        }
    }
    std::string names;
    for (const Lot& l : g_lots) names += (names.empty() ? "" : ", ") + l.name + " (" + std::to_string(l.layout.size()) + " park pieces)";
    Log(g_lots.empty() ? "no Skate 3 park lots (rerun SK8V setup)" : std::to_string(g_lots.size()) + " lots: " + names);
}
void SetObjects(std::function<std::vector<int>()> objects) { g_objects = std::move(objects); }
void SetRestore(std::function<bool(const Placed&)> restore) { g_restore = std::move(restore); }

void Enter(int lot) {
    const int ped = Call<int>(gta::PLAYER_PED_ID);
    if (lot < 0 || lot >= static_cast<int>(g_lots.size())) {
        Say("That skate.Park is not set up: rerun SK8V setup");
        return;
    }
    if (g_lot == lot) {
        Say("Already in " + g_lots[static_cast<std::size_t>(lot)].name);
        return;
    }
    g_falls = 0;
    if (CanWarp(ped)) Warp(lot);
}

void Exit() {
    const int ped = Call<int>(gta::PLAYER_PED_ID);
    if (g_lot < 0) {
        Say("Not in a skate.Park");
        return;
    }
    if (CanWarp(ped)) Warp(-1);
}

void Save() {
    if (g_lot < 0) {
        Say("Save Park works in a skate.Park");
        return;
    }
    std::vector<Placed> objects;
    for (int e : g_objects ? g_objects() : std::vector<int>{}) {
        if (!Call<BOOL>(gta::DOES_ENTITY_EXIST, e)) continue;
        Placed p;
        p.model = Call<std::uint32_t>(gta::GET_ENTITY_MODEL, e);
        const Vector3 at = Call<Vector3>(gta::GET_ENTITY_COORDS, e, 1);
        const Vector3 rot = Call<Vector3>(gta::GET_ENTITY_ROTATION, e, 2);
        p.pos[0] = at.x, p.pos[1] = at.y, p.pos[2] = at.z;
        p.rot[0] = rot.x, p.rot[1] = rot.y, p.rot[2] = rot.z;
        objects.push_back(p);
    }
    CreateDirectoryW(g_saveDir.c_str(), nullptr);
    std::ofstream out(SavePath(), std::ios::binary | std::ios::trunc);
    out << Serialize(objects);
    if (!out) {
        Say("Could not save the park");
        return;
    }
    Say(g_lots[static_cast<std::size_t>(g_lot)].name + " saved (" + std::to_string(objects.size()) + " objects)");
}

void Load() {
    if (g_lot < 0) {
        Say("Load Park works in a skate.Park");
        return;
    }
    std::ifstream in(SavePath(), std::ios::binary);
    if (!in) {
        Say("No saved park for " + g_lots[static_cast<std::size_t>(g_lot)].name + " yet");
        return;
    }
    std::stringstream text;
    text << in.rdbuf();
    std::vector<Placed> objects;
    if (!Parse(text.str(), objects)) {
        Say("The saved park could not be read");
        return;
    }
    if (!g_restore) {
        Say("Load Park needs the Object Dropper's restore (not in this build)");
        return;
    }
    std::size_t placed = 0;
    for (const Placed& p : objects) placed += g_restore(p) ? 1 : 0;
    Say("Park loaded (" + std::to_string(placed) + " of " + std::to_string(objects.size()) + " objects)");
}

bool Inside() { return g_lot >= 0; }
bool Busy() { return g_step != Step::Idle; }
const std::vector<int>& Objects() { return g_pieces; }
const std::vector<int>& Layout() { return g_layout; }
std::vector<std::string> Names() {
    std::vector<std::string> out;
    for (const Lot& l : g_lots) out.push_back(l.name);
    return out;
}

void Tick(bool skating, bool menus) {
    if (g_lots.empty()) return;
    Register();
    Blip();
    const int ped = Call<int>(gta::PLAYER_PED_ID);
    if (!Call<BOOL>(gta::DOES_ENTITY_EXIST, ped)) return;
    const DWORD since = GetTickCount() - g_stepSince;
    switch (g_step) {
    case Step::Idle: {
        if (g_lot >= 0) {
            const Lot& l = g_lots[static_cast<std::size_t>(g_lot)];
            const Vector3 at = Call<Vector3>(gta::GET_ENTITY_COORDS, ped, 1);
            const float dx = at.x - l.origin[0], dy = at.y - l.origin[1];
            if (dx * dx + dy * dy < l.reach * l.reach && at.z < l.origin[2] - kFallBelow && !Call<BOOL>(gta::IS_ENTITY_DEAD, ped, 0)) {
                if (++g_falls > kFalls) {
                    Say(l.name + "'s floor did not load: back by the hangar");
                    Warp(-1);
                } else {
                    Log("fell out of " + l.name + ": back to its spawn");
                    Warp(g_lot);
                }
                return;
            }
            // Gone some other way (death, a trainer's teleport, a mission): out after 3 s away,
            // not on the frame Skate takes the ped (its position reads elsewhere then, 2026-10-15).
            static DWORD outside = 0;
            if (InLot(l, at)) {
                outside = 0;
            } else if (!outside) {
                outside = GetTickCount();
            } else if (GetTickCount() - outside > 3000) {
                outside = 0;
                g_lot = -1;
                DeletePieces();
                Log("player left the lot without the exit");
            }
        }
        if (!Call<BOOL>(gta::IS_ENTITY_DEAD, ped, 0)) Markers(ped, menus);
        return;
    }
    case Step::FadeOut:
        if (!Call<BOOL>(gta::IS_SCREEN_FADED_OUT) && since < kFadeTimeout) return;
        // Into a lot: its pieces first (their models stream from anywhere), then the player.
        if (g_target >= 0 && g_piecesLot != g_target) {
            if (!g_registered) {
                Say("The skate.Park lots are not set up: rerun SK8V setup");
                Finish(ped, false);
                return;
            }
            if (!PiecesLoaded(g_target)) {
                if (since < kModelTimeout) return;
                Say(g_lots[static_cast<std::size_t>(g_target)].name + "'s models did not load");
                Finish(ped, false);
                return;
            }
            SpawnPieces(g_target);
        }
        Place(ped, skating);
        SetStep(Step::Settle);
        return;
    case Step::Settle: {
        Call<void>(gta::REQUEST_COLLISION_AT_COORD, g_to[0], g_to[1], g_to[2]);
        const bool floor = Call<BOOL>(gta::HAS_COLLISION_LOADED_AROUND_ENTITY, ped) != 0;
        // The lot's map streams with the focus; give its drawables a moment past the collision.
        if ((floor && since > kStreamMs) || since > kSettleTimeout) {
            if (!floor) Log("collision not reported loaded before the timeout");
            Finish(ped, true);
        }
        return;
    }
    }
}

}  // namespace parklot
