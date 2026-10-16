#include "object_dropper.h"
#include <windows.h>
#include <main.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <string>
#include "natives.h"
#include "streaming.h"

namespace dropper {
namespace {
using gta::Call;

// Skate 3's own dropper objects first, in its groups and order (setup builds
// them from the player's disc: tools/prepare-dropper.py, models sk3_*), then
// Create-a-Park's pieces (quarter pipes, mega ramps, funboxes, ledges; each in
// its Downtown, Industrial and University look), then GTA's skate-park props
// and street furniture. Every one has an exact template
// in the world cache. Ramps face local -Y, so a skater rolling along the
// prop's +Y meets the ramp (yaw 0); setup turns Skate 3's objects that way and
// lays their long ones along Y. GTA's long spots lie along local X and turn a
// quarter so their edges run along the skater's line. GTA's skate props are
// their *_cr copies: only those carry the archetype flag CREATE_OBJECT needs.
enum Category {
    kSkate0, kSkate1, kSkate2, kSkate3, kSkate4, kSkate5, kSkate6,
    kPark0, kPark1, kPark2, kPark3, kPark4, kPark5, kPark6, kPark7, kPark8, kPark9, kPark10, kPark11,
    kGta, kCategoryCount
};
constexpr const char* kCategoryNames[kCategoryCount] = {
    "Ramps", "Rails", "Benches & Barriers", "Tables", "Bins", "Boxes", "Misc",
    "Vert", "Street", "Mega", "Pre-made", "Ledges", "Park Rails", "Plaza", "Catwalks & Misc", "Street Furniture",
    "Landmarks", "Branded", "Foliage",
    "GTA Props"};
// The screen's description box, per group.
constexpr const char* kCategoryDescriptions[kCategoryCount] = {
    "Quarter pipes, kickers, wedges and manual pads", "Flat, kinked and hi-lo rails", "Benches, ledges and barriers to grind",
    "Tables to grind and manual", "Bins and dumpsters", "Crates and boxes", "Everything else",
    "Vert pads, quarter pipes, corners, hips, roll-ins and extensions", "Street pads, quarter pipes, banks, ramps and stairs",
    "Mega ramp roll-ins, launches, landings, quarter pipes and full pipes", "Ready-made quarter pipes, funboxes and bowl stairs",
    "Ledges, down ledges and hubbas", "Round and square rails, down rails, fences and rainbow rails",
    "Plaza pads, big stairs, planters, grass and water boxes", "Catwalks, fences, jumbotrons and grandstands",
    "Benches, lamp posts, ad boards and awnings", "Signs, planters, vehicles and pieces of the city",
    "Billboards, flags, tents and media towers", "Trees, palms, bushes and hedges",
    "Los Santos skate park props and street furniture"};
// floor: the model z that sits on the ground (the _cr copies have buried bases); kLowest: its lowest point.
struct Item { const char* model; const char* name; float yaw; float floor; Category category; };
constexpr float kLowest = NAN;
constexpr Item kItems[] = {
#include "dropper_items.inc"
    {"prop_skate_kickers_cr", "Kicker", 0.0f, 0.0f, kGta},
    {"prop_mp_ramp_03", "Launch Ramp", 0.0f, kLowest, kGta},
    {"prop_mp_ramp_02", "Small Launch Ramp", 0.0f, kLowest, kGta},
    {"prop_skate_flatramp_cr", "Flat Bank", 0.0f, 0.0f, kGta},
    {"prop_skate_quartpipe_cr", "Quarter Pipe", 0.0f, -1.42f, kGta},
    {"prop_skate_spiner_cr", "Spine", 0.0f, 0.0f, kGta},
    {"prop_skate_funbox_cr", "Funbox", 0.0f, 0.0f, kGta},
    {"prop_skate_halfpipe_cr", "Half Pipe", 0.0f, 0.0f, kGta},
    {"prop_beach_dip_bars_02", "Flat Bars", 90.0f, 0.0f, kGta},
    {"prop_beach_dip_bars_01", "High Bars", 90.0f, 0.0f, kGta},
    {"prop_bench_01a", "Bench", 90.0f, kLowest, kGta},
    {"prop_picnictable_01", "Picnic Table", 90.0f, kLowest, kGta},
    {"prop_barier_conc_02a", "Jersey Barrier", 90.0f, kLowest, kGta},
    {"prop_dumpster_01a", "Dumpster", 90.0f, kLowest, kGta},
};
constexpr int kItemCount = static_cast<int>(sizeof(kItems) / sizeof(kItems[0]));
// The screen's tabs (retail: Props, Terrain): Skate 3's dropper groups, Create-a-Park's, then GTA's.
struct Tab { const char* name; Category first, last; };
constexpr Tab kTabs[] = {{"Props", kSkate0, kSkate6}, {"Park", kPark0, kPark11}, {"GTA", kGta, kGta}};
// Gap from the skater to the near edge of a new ghost.
constexpr float kAhead = 3.0f;
constexpr DWORD kLoadTimeoutMs = 5000;
// Ghost handling per second (fine tune: a quarter).
constexpr float kMoveSpeed = 4.0f, kTurnSpeed = 120.0f, kLiftSpeed = 1.0f, kFine = 0.25f;
constexpr float kDeadZone = 0.25f;
// Y deletes the nearest dropped object within this many metres of the ghost.
constexpr float kDeleteRange = 6.0f;

constexpr std::uint16_t kUp = 0x0001, kDown = 0x0002, kLeft = 0x0004, kRight = 0x0008, kL3 = 0x0040;
constexpr std::uint16_t kLB = 0x0100, kA = 0x1000, kB = 0x2000, kX = 0x4000, kY = 0x8000;

// Retail's FEObjectDropperScreenMode values, in skate_hud::dropper::MODES order.
enum Mode : std::uint32_t {
    kCatalog = 0, kGotoItems = 3, kGotoSubCategories = 4, kManipulate = 7, kNextCategory = 9, kNextItem = 10,
    kNextSubCategory = 11, kPreviousCategory = 12, kPreviousItem = 13, kPreviousSubCategory = 14, kRefresh = 15,
    kRefreshDuplicate = 16, kRefreshFineTune = 18,
};

enum class Stage { Closed, Categories, Items, Placing };
Stage g_stage = Stage::Closed;
int g_tab = 0;
int g_category = kSkate0;
int g_pick = 0; // into kItems, in g_category
bool g_duplicate = false, g_fine = false;
std::deque<std::uint32_t> g_modes; // sent one per frame
std::uint32_t g_mode = kCatalog, g_serial = 0;
bool (*g_present)(const SvObjectDropper&) = nullptr;
bool g_presented = false, g_sentOpen = false;
// Placement inputs, read in Controls and applied in Tick (frame time, camera).
struct Input { float lx, ly, rx; bool rt; int lift; std::uint16_t pressed; } g_in{};
// The ghost: its object (0 until the model is in), where it is.
int g_ghost = 0;
Hash g_ghostModel = 0;
DWORD g_ghostSince = 0;
float g_gx = 0.0f, g_gy = 0.0f, g_gz = 0.0f, g_heading = 0.0f, g_lift = 0.0f;
bool g_ghostPlaced = false; // its position is set (else it starts ahead of the skater)
std::vector<int> g_objects;
struct Restoring { Hash model; SvVec3 pos, rot; DWORD since; };
std::deque<Restoring> g_restore;

void (*g_log)(const char*) = nullptr;
void Log(const char* s) { if (g_log) g_log(s); }

// GTA's model name hash (joaat, lower case).
Hash ModelHash(const char* name) {
    std::uint32_t h = 0;
    for (const char* c = name; *c; ++c) {
        h += static_cast<std::uint8_t>(*c >= 'A' && *c <= 'Z' ? *c + 32 : *c);
        h += h << 10;
        h ^= h >> 6;
    }
    h += h << 3;
    h ^= h >> 11;
    h += h << 15;
    return static_cast<Hash>(h);
}

// Skate 3's objects: <DataRoot>/private/dropper (setup), registered once per
// process like the native board (registrations persist).
std::filesystem::path g_dir;
bool g_registered = false, g_registerFailed = false;
bool g_available[kItemCount] = {};

void Register() {
    if (g_registered || g_registerFailed || g_dir.empty() || !streaming::Ready()) return;
    const auto ytyp = g_dir / L"sk3_dropper.ytyp";
    const bool present = std::filesystem::is_regular_file(ytyp);
    if (!present || !streaming::Guards()) {
        g_registerFailed = true;
        Log(present ? "SkateV Legacy: object dropper: 3889 streaming function guards differ"
                    : "SkateV Legacy: object dropper: no Skate 3 objects (rerun SK8V setup)");
        return;
    }
    const auto reg = streaming::Registrar();
    int count = 0;
    for (const Item& item : kItems) {
        if (item.category == kGta) continue;
        const auto ydr = g_dir / (std::string(item.model) + ".ydr");
        const std::string path = streaming::NarrowAnsi(ydr), name = std::string(item.model) + ".ydr";
        if (path.empty() || !std::filesystem::is_regular_file(ydr)) continue;
        std::uint32_t id = ~0u;
        reg(&id, path.c_str(), true, name.c_str(), false, false);
        if (id != ~0u) ++count;
    }
    const std::string path = streaming::NarrowAnsi(ytyp);
    std::uint32_t id = ~0u;
    if (!path.empty()) reg(&id, path.c_str(), true, "sk3_dropper.ytyp", false, false);
    if (id == ~0u) {
        g_registerFailed = true;
        Log("SkateV Legacy: object dropper: Skate 3 object types did not register");
        return;
    }
    streaming::TypeLoader()("sk3_dropper.ytyp");
    g_registered = true;
    char line[128];
    std::snprintf(line, sizeof(line), "SkateV Legacy: object dropper: %d Skate 3 objects registered", count);
    Log(line);
}

// Which items GTA can create (a Skate 3 object needs setup's model).
void RefreshAvailable() {
    for (int i = 0; i < kItemCount; ++i) g_available[i] = Call<BOOL>(gta::IS_MODEL_IN_CDIMAGE, ModelHash(kItems[i].model)) != 0;
}

bool CategoryHas(int category) {
    for (int i = 0; i < kItemCount; ++i)
        if (kItems[i].category == category && g_available[i]) return true;
    return false;
}

// The tab's groups that have something to drop.
std::vector<int> Categories(int tab) {
    std::vector<int> out;
    for (int c = kTabs[tab].first; c <= kTabs[tab].last; ++c)
        if (CategoryHas(c)) out.push_back(c);
    return out;
}

std::vector<int> Items(int category) {
    std::vector<int> out;
    for (int i = 0; i < kItemCount; ++i)
        if (kItems[i].category == category && g_available[i]) out.push_back(i);
    return out;
}

constexpr int kTabCount = static_cast<int>(sizeof(kTabs) / sizeof(kTabs[0]));

// Moves the tab by `step` (0: keeps it) to one with something to drop, and
// its group and item to ones that exist there.
bool FixSelection(int step) {
    for (int n = 0; n < kTabCount; ++n) {
        if (n || step) g_tab = (g_tab + (step < 0 ? -1 : 1) + kTabCount) % kTabCount;
        const auto categories = Categories(g_tab);
        if (categories.empty()) continue;
        if (std::find(categories.begin(), categories.end(), g_category) == categories.end()) g_category = categories.front();
        const auto items = Items(g_category);
        if (std::find(items.begin(), items.end(), g_pick) == items.end()) g_pick = items.front();
        return true;
    }
    return false;
}

void Mode(std::uint32_t mode) { g_modes.push_back(mode); }

// One step through `list` from `current` (wrapping).
int Step(const std::vector<int>& list, int current, int step) {
    const auto it = std::find(list.begin(), list.end(), current);
    const int at = it == list.end() ? 0 : static_cast<int>(it - list.begin());
    return list[(at + step + static_cast<int>(list.size())) % list.size()];
}

void Delete(int object) {
    if (!Call<BOOL>(gta::DOES_ENTITY_EXIST, object)) return;
    Call<void>(gta::SET_ENTITY_AS_MISSION_ENTITY, object, 1, 1);
    Call<void>(gta::DELETE_OBJECT, &object);
}

void Keep(int object) {
    g_objects.push_back(object);
    while (g_objects.size() > kMaxObjects) {
        Delete(g_objects.front());
        g_objects.erase(g_objects.begin());
    }
}

// A dropped object: frozen, ours (mission entity), in Objects().
int Create(Hash model, float x, float y, float z) {
    const int object = Call<int>(gta::CREATE_OBJECT_NO_OFFSET, model, x, y, z, false, false, false, 0);
    if (object) {
        Call<void>(gta::SET_ENTITY_AS_MISSION_ENTITY, object, 1, 1);
        Call<void>(gta::FREEZE_ENTITY_POSITION, object, 1);
    }
    return object;
}

void DropGhost() {
    if (g_ghost) Delete(g_ghost);
    g_ghost = 0;
    if (g_ghostModel) Call<void>(gta::SET_MODEL_AS_NO_LONGER_NEEDED, g_ghostModel);
    g_ghostModel = 0;
    g_ghostPlaced = false;
}

void Close(const char* why) {
    if (g_stage == Stage::Closed) return;
    DropGhost();
    g_stage = Stage::Closed;
    g_modes.clear();
    (void)why;
}

void StartPlacing() {
    const Hash model = ModelHash(kItems[g_pick].model);
    if (!Call<BOOL>(gta::IS_MODEL_IN_CDIMAGE, model)) return;
    DropGhost();
    Call<void>(gta::REQUEST_MODEL, model);
    g_ghostModel = model;
    g_ghostSince = GetTickCount();
    g_lift = 0.0f;
    g_stage = Stage::Placing;
    Mode(kManipulate);
}

// The ground under (x, y) near height `z`, else `z`.
float Ground(float x, float y, float z) {
    float ground = 0.0f;
    if (!Call<BOOL>(gta::GET_GROUND_Z_FOR_3D_COORD, x, y, z + 3.0f, &ground, 0, 0) || !std::isfinite(ground) || std::fabs(ground - z) > 6.0f)
        return z;
    return ground;
}

float Base(const Item& item, Hash model) {
    Vector3 lo{}, hi{};
    Call<void>(gta::GET_MODEL_DIMENSIONS, model, &lo, &hi);
    return std::isnan(item.floor) ? lo.z : item.floor;
}

// Moves the ghost with this frame's input; creates it once its model is in.
void Place(SvVec3 skater, float heading) {
    const Item& item = kItems[g_pick];
    if (!g_ghost) {
        if (!Call<BOOL>(gta::HAS_MODEL_LOADED, g_ghostModel)) {
            if (GetTickCount() - g_ghostSince > kLoadTimeoutMs) {
                Log("SkateV Legacy: object dropper: model did not load in time");
                g_stage = Stage::Items;
                Mode(kGotoItems);
                DropGhost();
            }
            return;
        }
        if (!g_ghostPlaced) {
            Vector3 lo{}, hi{};
            Call<void>(gta::GET_MODEL_DIMENSIONS, g_ghostModel, &lo, &hi);
            const float rad = heading * 0.01745329f;
            const float ahead = kAhead - (item.yaw != 0.0f ? lo.x : lo.y);
            g_gx = skater.x - std::sin(rad) * ahead;
            g_gy = skater.y + std::cos(rad) * ahead;
            g_gz = skater.z;
            g_heading = std::fmod(heading + item.yaw + 360.0f, 360.0f);
            g_ghostPlaced = true;
        }
        g_ghost = Call<int>(gta::CREATE_OBJECT_NO_OFFSET, g_ghostModel, g_gx, g_gy, g_gz, false, false, false, 0);
        if (!g_ghost) {
            Log("SkateV Legacy: object dropper: ghost failed to spawn");
            g_stage = Stage::Items;
            Mode(kGotoItems);
            DropGhost();
            return;
        }
        Call<void>(gta::SET_ENTITY_AS_MISSION_ENTITY, g_ghost, 1, 1);
        Call<void>(gta::FREEZE_ENTITY_POSITION, g_ghost, 1);
        Call<void>(gta::SET_ENTITY_COLLISION, g_ghost, 0, 0);
        Call<void>(gta::SET_ENTITY_ALPHA, g_ghost, 160, 0);
    }
    const float dt = (std::min)(Call<float>(gta::GET_FRAME_TIME), 0.1f);
    const float k = g_fine ? kFine : 1.0f;
    // Left stick: camera relative (RT held: it turns instead).
    const Vector3 cam = Call<Vector3>(gta::GET_FINAL_RENDERED_CAM_ROT, 2);
    const float ch = cam.z * 0.01745329f;
    if (g_in.rt) {
        g_heading -= g_in.lx * kTurnSpeed * k * dt;
    } else {
        const float fx = -std::sin(ch), fy = std::cos(ch), rx = std::cos(ch), ry = std::sin(ch);
        g_gx += (fx * g_in.ly + rx * g_in.lx) * kMoveSpeed * k * dt;
        g_gy += (fy * g_in.ly + ry * g_in.lx) * kMoveSpeed * k * dt;
    }
    g_heading = std::fmod(g_heading - g_in.rx * kTurnSpeed * k * dt + 360.0f, 360.0f);
    g_lift = std::clamp(g_lift + g_in.lift * kLiftSpeed * k * dt, -2.0f, 8.0f);
    if (g_in.pressed & kL3) {
        g_ghostPlaced = false;
        g_lift = 0.0f;
        Delete(g_ghost);
        g_ghost = 0;
        return;
    }
    g_gz = Ground(g_gx, g_gy, g_gz + Base(item, g_ghostModel) - g_lift) - Base(item, g_ghostModel) + g_lift;
    Call<void>(gta::SET_ENTITY_COORDS_NO_OFFSET, g_ghost, g_gx, g_gy, g_gz, 0, 0, 0);
    Call<void>(gta::SET_ENTITY_HEADING, g_ghost, g_heading);
    if (g_in.pressed & kY) {
        // The nearest dropped object goes.
        int best = -1;
        float bestD = kDeleteRange * kDeleteRange;
        for (int i = 0; i < static_cast<int>(g_objects.size()); ++i) {
            const Vector3 p = Call<Vector3>(gta::GET_ENTITY_COORDS, g_objects[i], 0);
            const float d = (p.x - g_gx) * (p.x - g_gx) + (p.y - g_gy) * (p.y - g_gy) + (p.z - g_gz) * (p.z - g_gz);
            if (d < bestD) bestD = d, best = i;
        }
        if (best >= 0) {
            Delete(g_objects[best]);
            g_objects.erase(g_objects.begin() + best);
        }
    }
    if (g_in.pressed & kA) {
        const int object = Create(g_ghostModel, g_gx, g_gy, g_gz);
        if (object) {
            Call<void>(gta::SET_ENTITY_HEADING, object, g_heading);
            Keep(object);
            char line[200];
            std::snprintf(line, sizeof(line), "SkateV Legacy: object dropper: %s at (%.1f, %.1f, %.1f) heading %.0f, %zu out",
                          item.model, g_gx, g_gy, g_gz, g_heading, g_objects.size());
            Log(line);
        } else {
            Log("SkateV Legacy: object dropper: object failed to spawn");
        }
        // Duplicate keeps a copy to place again; else back to skating.
        if (!g_duplicate) Close("placed");
    }
}

// Saved parks: one object a frame once its model is in.
void RestoreTick() {
    if (g_restore.empty()) return;
    Restoring& r = g_restore.front();
    if (!r.since) {
        r.since = GetTickCount();
        Call<void>(gta::REQUEST_MODEL, r.model);
    }
    if (Call<BOOL>(gta::HAS_MODEL_LOADED, r.model)) {
        const int object = Create(r.model, r.pos.x, r.pos.y, r.pos.z);
        if (object) {
            Call<void>(gta::SET_ENTITY_ROTATION, object, r.rot.x, r.rot.y, r.rot.z, 2, 1);
            Keep(object);
        }
        Call<void>(gta::SET_MODEL_AS_NO_LONGER_NEEDED, r.model);
        g_restore.pop_front();
    } else if (GetTickCount() - r.since > kLoadTimeoutMs) {
        Call<void>(gta::SET_MODEL_AS_NO_LONGER_NEEDED, r.model);
        g_restore.pop_front();
        Log("SkateV Legacy: object dropper: a restored object's model did not load");
    }
}

// The screen's state for the runtime (sent every frame while open, once when it closes).
void Present() {
    const bool open = g_stage != Stage::Closed;
    if (!g_present || (!open && !g_sentOpen)) {
        g_presented = false;
        return;
    }
    if (open && !g_modes.empty()) {
        g_mode = g_modes.front();
        g_modes.pop_front();
        ++g_serial;
    }
    std::string lists;
    auto group = [&](const std::vector<std::string>& entries) {
        for (std::size_t i = 0; i < entries.size(); ++i) {
            if (i) lists += '\x1f';
            lists += entries[i];
        }
    };
    std::vector<std::string> tabs, categories, descriptions, items;
    int tab = 0, category = 0, item = 0;
    for (int t = 0; t < kTabCount; ++t) {
        if (Categories(t).empty()) continue;
        if (t == g_tab) tab = static_cast<int>(tabs.size());
        tabs.push_back(kTabs[t].name);
    }
    for (const int c : Categories(g_tab)) {
        if (c == g_category) category = static_cast<int>(categories.size());
        categories.push_back(kCategoryNames[c]);
        descriptions.push_back(kCategoryDescriptions[c]);
    }
    for (const int i : Items(g_category)) {
        if (i == g_pick) item = static_cast<int>(items.size());
        items.push_back(kItems[i].name);
    }
    group(tabs);
    lists += '\x1e';
    group(categories);
    lists += '\x1e';
    group(descriptions);
    lists += '\x1e';
    group(items);
    SvObjectDropper d{};
    d.size = sizeof(d);
    d.visible = open ? 1 : 0;
    d.mode = g_mode;
    d.mode_serial = g_serial;
    d.tab = static_cast<std::uint32_t>(tab);
    d.category = static_cast<std::uint32_t>(category);
    d.item = static_cast<std::uint32_t>(item);
    d.flags = (g_stage == Stage::Categories ? SV_DROPPER_CHOOSING_CATEGORY : 0) | (g_stage == Stage::Placing ? SV_DROPPER_PLACING : 0) |
              (g_duplicate ? SV_DROPPER_DUPLICATE : 0) | (g_fine ? SV_DROPPER_FINE_TUNE : 0);
    d.lists_utf8 = lists.c_str();
    g_presented = g_present(d);
    g_sentOpen = open;
}
} // namespace

void Controls(SvPad& pad, std::uint16_t pressed) {
    static bool ltWas = false, rtWas = false;
    const std::uint16_t buttons = pad.buttons;
    const bool lt = pad.left_trigger > 100, rt = pad.right_trigger > 100;
    const bool ltPressed = lt && !ltWas, rtPressed = rt && !rtWas;
    ltWas = lt, rtWas = rt;
    if (g_stage == Stage::Closed) {
        if ((buttons & kLB) && (pressed & kB)) {
            RefreshAvailable();
            if (FixSelection(0)) {
                g_stage = Stage::Items;
                g_modes.clear();
                Mode(kCatalog);
                Mode(kGotoItems);
            } else {
                Log("SkateV Legacy: object dropper: nothing to drop");
            }
        }
        // LB + B is the dropper's: Skate never sees B under LB.
        if (buttons & kLB) pad.buttons &= static_cast<std::uint16_t>(~kB);
        if (g_stage == Stage::Closed) return;
        pressed &= static_cast<std::uint16_t>(~kB); // the press that opened it
    }
    switch (g_stage) {
    case Stage::Categories:
    case Stage::Items: {
        const bool items = g_stage == Stage::Items;
        if (ltPressed || rtPressed) {
            const int before = g_tab;
            FixSelection(rtPressed ? 1 : -1);
            if (g_tab != before) {
                g_stage = Stage::Categories;
                Mode(rtPressed ? kNextCategory : kPreviousCategory);
                Mode(kGotoSubCategories);
            }
        } else if (pressed & (kUp | kDown)) {
            const int step = (pressed & kDown) ? 1 : -1;
            if (items) {
                g_pick = Step(Items(g_category), g_pick, step);
                Mode(step > 0 ? kNextItem : kPreviousItem);
            } else {
                g_category = Step(Categories(g_tab), g_category, step);
                g_pick = Items(g_category).front();
                Mode(step > 0 ? kNextSubCategory : kPreviousSubCategory);
            }
        } else if (pressed & kA) {
            if (items) {
                StartPlacing();
            } else {
                g_stage = Stage::Items;
                Mode(kGotoItems);
            }
        } else if (pressed & kB) {
            if (items) {
                g_stage = Stage::Categories;
                Mode(kGotoSubCategories);
            } else {
                Close("back");
            }
        }
        break;
    }
    case Stage::Placing: {
        auto axis = [](std::int16_t v) {
            const float f = std::clamp(v / 32767.0f, -1.0f, 1.0f);
            return std::fabs(f) < kDeadZone ? 0.0f : f;
        };
        g_in = {axis(pad.left_x), axis(pad.left_y), axis(pad.right_x), rt,
                (buttons & kUp) ? 1 : (buttons & kDown) ? -1 : 0, pressed};
        if (pressed & kLB) {
            g_duplicate = !g_duplicate;
            Mode(kRefreshDuplicate);
        }
        if (lt != g_fine) {
            g_fine = lt;
            Mode(kRefreshFineTune);
        }
        if (pressed & (kB | kX)) {
            DropGhost();
            g_in = {};
            g_stage = Stage::Items;
            Mode(kCatalog);
            Mode(kGotoItems);
        }
        break;
    }
    case Stage::Closed:
        break;
    }
    // The dropper has the pad while it is open (Skate is paused).
    if (g_stage != Stage::Closed) {
        pad.buttons = 0;
        pad.left_trigger = pad.right_trigger = 0;
        pad.left_x = pad.left_y = pad.right_x = pad.right_y = 0;
    }
}

void Tick(SvVec3 skater, float heading) {
    Register();
    for (std::size_t i = 0; i < g_objects.size();) {
        if (Call<BOOL>(gta::DOES_ENTITY_EXIST, g_objects[i])) ++i;
        else g_objects.erase(g_objects.begin() + static_cast<std::ptrdiff_t>(i));
    }
    RestoreTick();
    if (g_stage == Stage::Placing && std::isfinite(skater.x) && std::isfinite(skater.y) && std::isfinite(skater.z) && std::isfinite(heading))
        Place(skater, heading);
    g_in.pressed = 0;
    Present();
}

void Draw() {
    if (g_stage == Stage::Closed || g_presented) return;
    char line[160];
    gta::Text("OBJECT DROPPER", 0.5f, 0.715f, 0.42f, 255, 255, 255, 255, gta::Align::Centre);
    std::snprintf(line, sizeof(line), "%s  >  %s", kTabs[g_tab].name, kCategoryNames[g_category]);
    gta::Text(line, 0.5f, 0.745f, 0.36f, 255, 255, 255, 255, gta::Align::Centre);
    if (g_stage == Stage::Placing) {
        std::snprintf(line, sizeof(line), "%s", kItems[g_pick].name);
        gta::Text(line, 0.5f, 0.775f, 0.6f, 255, 214, 0, 255, gta::Align::Centre);
        std::snprintf(line, sizeof(line), "LS move  RS turn  D-pad raise  A place  LB duplicate %s  LT fine  Y delete  B back",
                      g_duplicate ? "on" : "off");
    } else {
        std::snprintf(line, sizeof(line), "%s%s", g_stage == Stage::Categories ? "> " : "", kItems[g_pick].name);
        gta::Text(line, 0.5f, 0.775f, 0.6f, 255, 214, 0, 255, gta::Align::Centre);
        std::snprintf(line, sizeof(line), "Up/Down pick   LT/RT tab   A select   B back   (%zu/%zu out)", g_objects.size(), kMaxObjects);
    }
    gta::Text(line, 0.5f, 0.825f, 0.32f, 230, 230, 230, 255, gta::Align::Centre);
}

void SetLog(void (*log)(const char*)) { g_log = log; }
void SetDataRoot(const std::wstring& dataRoot) {
    if (dataRoot.empty() || g_registered) return; // registered paths stay for the process
    g_dir = std::filesystem::path(dataRoot) / L"private" / L"dropper";
    g_registerFailed = false;
}
void SetPresenter(bool (*present)(const SvObjectDropper&)) { g_present = present; }
bool IsOpen() { return g_stage != Stage::Closed; }
void Close() { Close("closed"); }
int Ghost() { return g_ghost; }
const std::vector<int>& Objects() { return g_objects; }

bool Restore(std::uint32_t model, SvVec3 pos, SvVec3 rotDeg) {
    if (!Call<BOOL>(gta::IS_MODEL_IN_CDIMAGE, static_cast<Hash>(model))) return false;
    g_restore.push_back({static_cast<Hash>(model), pos, rotDeg, 0});
    return true;
}

void Clear() {
    for (const int object : g_objects) Delete(object);
    if (!g_objects.empty()) Log("SkateV Legacy: object dropper: dropped objects removed");
    g_objects.clear();
    g_restore.clear();
    Close("cleared");
    Present();
}

} // namespace dropper
