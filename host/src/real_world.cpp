// Real-life clock and weather (see real_world.h).
#include "real_world.h"
#include <windows.h>
#include <winhttp.h>
#include <main.h>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <thread>
#include "host_util.h"
#include "natives.h"

namespace realworld {

const char* WeatherFor(int wmo, double cloudPercent, double windKmh) {
    switch (wmo) {
    case 0: return cloudPercent < 10 ? "EXTRASUNNY" : "CLEAR";
    case 1: return "CLEAR";
    case 2: return "CLOUDS";
    case 3: return "OVERCAST";
    case 45: case 48: return "FOGGY";
    case 51: case 53: case 55: case 56: case 57: return "CLEARING";  // drizzle: GTA's light rain
    case 61: case 63: case 65: case 66: case 67: case 80: case 81: case 82: return "RAIN";
    case 71: case 77: case 85: return "SNOWLIGHT";
    case 73: return windKmh >= 40 ? "BLIZZARD" : "SNOWLIGHT";
    case 75: case 86: return windKmh >= 40 ? "BLIZZARD" : "SNOW";
    case 95: case 96: case 99: return "THUNDER";
    default: return nullptr;
    }
}

const char* Describe(int wmo) {
    switch (wmo) {
    case 0: return "Clear sky";
    case 1: return "Mainly clear";
    case 2: return "Partly cloudy";
    case 3: return "Overcast";
    case 45: case 48: return "Fog";
    case 51: case 53: case 55: return "Drizzle";
    case 56: case 57: return "Freezing drizzle";
    case 61: return "Light rain";
    case 63: return "Rain";
    case 65: return "Heavy rain";
    case 66: case 67: return "Freezing rain";
    case 71: return "Light snow";
    case 73: return "Snow";
    case 75: return "Heavy snow";
    case 77: return "Snow grains";
    case 80: case 81: case 82: return "Rain showers";
    case 85: case 86: return "Snow showers";
    case 95: return "Thunderstorm";
    case 96: case 99: return "Thunderstorm with hail";
    default: return "Unknown weather";
    }
}

bool JsonNumber(const std::string& json, const char* key, double& out, std::size_t from) {
    const std::string quoted = std::string("\"") + key + "\"";
    std::size_t at = json.find(quoted, from);
    if (at == std::string::npos) return false;
    at = json.find(':', at + quoted.size());
    if (at == std::string::npos) return false;
    const char* start = json.c_str() + at + 1;
    while (*start == ' ' || *start == '\t' || *start == '\r' || *start == '\n') ++start;
    char* end = nullptr;
    const double v = std::strtod(start, &end);
    if (end == start) return false;
    out = v;
    return true;
}

namespace {
using gta::Call;

LogFn g_log = nullptr;
std::function<void(const std::string&)> g_notify;
Options g_opt;

// What the worker thread hands the script thread (shared: a fetch may outlive a reconfigure).
struct Shared {
    std::mutex lock;
    bool busy = false;
    bool fresh = false;        // a result not yet taken by Tick
    bool ok = false;
    std::string weather;       // GTA weather type
    std::string status;        // in words, for Status()
    // Resolved WeatherLocation, kept until the setting changes.
    std::string resolvedFor;
    bool resolved = false;
    double lat = 0, lon = 0;
    std::string place;
};
std::shared_ptr<Shared> g_shared = std::make_shared<Shared>();

// HTTPS GET with short timeouts; "" on any failure.
std::string HttpGet(const std::wstring& url) {
    URL_COMPONENTS uc{};
    uc.dwStructSize = sizeof(uc);
    wchar_t host[256]{}, path[1024]{};
    uc.lpszHostName = host, uc.dwHostNameLength = 256;
    uc.lpszUrlPath = path, uc.dwUrlPathLength = 1024;
    wchar_t extra[1024]{};
    uc.lpszExtraInfo = extra, uc.dwExtraInfoLength = 1024;
    if (!WinHttpCrackUrl(url.c_str(), 0, 0, &uc)) return {};
    std::string body;
    HINTERNET session = WinHttpOpen(L"SK8V/1.0", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, WINHTTP_NO_PROXY_NAME,
                                    WINHTTP_NO_PROXY_BYPASS, 0);
    if (!session) return {};
    WinHttpSetTimeouts(session, 5000, 5000, 8000, 8000);
    HINTERNET connect = WinHttpConnect(session, host, uc.nPort, 0);
    HINTERNET request = connect ? WinHttpOpenRequest(connect, L"GET", (std::wstring(path) + extra).c_str(), nullptr,
                                                     WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                                     uc.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0)
                                : nullptr;
    DWORD statusCode = 0, size = sizeof(statusCode);
    if (request && WinHttpSendRequest(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0) &&
        WinHttpReceiveResponse(request, nullptr) &&
        WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX,
                            &statusCode, &size, WINHTTP_NO_HEADER_INDEX) &&
        statusCode == 200) {
        for (DWORD avail = 0; WinHttpQueryDataAvailable(request, &avail) && avail && body.size() < (1u << 20);) {
            std::string chunk(avail, '\0');
            DWORD read = 0;
            if (!WinHttpReadData(request, chunk.data(), avail, &read) || !read) break;
            body.append(chunk.data(), read);
        }
    }
    if (request) WinHttpCloseHandle(request);
    if (connect) WinHttpCloseHandle(connect);
    WinHttpCloseHandle(session);
    return body;
}

std::wstring UrlEncode(const std::string& s) {
    std::wstring out;
    for (unsigned char c : s) {
        if (std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
            out += static_cast<wchar_t>(c);
        } else {
            wchar_t buf[4];
            swprintf(buf, 4, L"%%%02X", c);
            out += buf;
        }
    }
    return out;
}

// The text after "key":" in json (no escapes beyond what place names need).
std::string JsonString(const std::string& json, const char* key) {
    const std::string quoted = std::string("\"") + key + "\"";
    std::size_t at = json.find(quoted);
    if (at == std::string::npos) return {};
    at = json.find('"', json.find(':', at + quoted.size()));
    if (at == std::string::npos) return {};
    const std::size_t end = json.find('"', at + 1);
    return end == std::string::npos ? std::string() : json.substr(at + 1, end - at - 1);
}

// WeatherLocation -> coordinates: "lat,lon", a city name, or the IP's location.
bool Resolve(const std::string& where, double& lat, double& lon, std::string& place, std::string& why) {
    char* end = nullptr;
    lat = std::strtod(where.c_str(), &end);
    if (end != where.c_str() && *end == ',') {
        const char* second = end + 1;
        lon = std::strtod(second, &end);
        if (end != second) {
            place = where;
            return true;
        }
    }
    if (!where.empty()) {
        const std::string json = HttpGet(L"https://geocoding-api.open-meteo.com/v1/search?count=1&format=json&name=" + UrlEncode(where));
        const std::size_t results = json.find("\"results\"");
        if (json.empty()) return why = "offline (geocoder unreachable)", false;
        if (results == std::string::npos || !JsonNumber(json, "latitude", lat, results) || !JsonNumber(json, "longitude", lon, results))
            return why = "WeatherLocation '" + where + "' not found", false;
        place = JsonString(json.substr(results), "name");
        return true;
    }
    const std::string json = HttpGet(L"https://ipwho.is/?fields=success,latitude,longitude,city");
    if (json.empty()) return why = "offline (location service unreachable)", false;
    std::size_t ok = json.find("\"success\"");
    if (ok != std::string::npos)
        for (ok += 9; ok < json.size() && (json[ok] == ':' || std::isspace(static_cast<unsigned char>(json[ok]))); ++ok) {}
    if (ok >= json.size()) ok = std::string::npos;
    if (ok == std::string::npos || json[ok] != 't' || !JsonNumber(json, "latitude", lat) || !JsonNumber(json, "longitude", lon))
        return why = "location from IP unavailable; set WeatherLocation in SkateVLegacy.ini", false;
    place = JsonString(json, "city");
    return true;
}

void Fetch(std::shared_ptr<Shared> sh, std::string where) {
    double lat = 0, lon = 0;
    std::string place, why;
    bool have = false;
    {
        std::lock_guard<std::mutex> l(sh->lock);
        if (sh->resolved && sh->resolvedFor == where) lat = sh->lat, lon = sh->lon, place = sh->place, have = true;
    }
    if (!have && Resolve(where, lat, lon, place, why)) {
        have = true;
        std::lock_guard<std::mutex> l(sh->lock);
        sh->resolved = true, sh->resolvedFor = where, sh->lat = lat, sh->lon = lon, sh->place = place;
    }
    bool ok = false;
    std::string weather;
    if (have) {
        wchar_t url[256];
        swprintf(url, 256,
                 L"https://api.open-meteo.com/v1/forecast?latitude=%.3f&longitude=%.3f"
                 L"&current=weather_code,cloud_cover,wind_speed_10m,temperature_2m",
                 lat, lon);
        const std::string json = HttpGet(url);
        const std::size_t current = json.find("\"current\":");
        double code = 0, cloud = 0, wind = 0, temp = 0;
        if (json.empty()) {
            why = "offline (Open-Meteo unreachable)";
        } else if (current == std::string::npos || !JsonNumber(json, "weather_code", code, current)) {
            why = "Open-Meteo sent no current weather";
        } else {
            JsonNumber(json, "cloud_cover", cloud, current);
            JsonNumber(json, "wind_speed_10m", wind, current);
            JsonNumber(json, "temperature_2m", temp, current);
            const int wmo = static_cast<int>(code);
            const char* type = WeatherFor(wmo, cloud, wind);
            char text[200];
            std::snprintf(text, sizeof text, "%s, %.0f C%s%s%s", Describe(wmo), temp, place.empty() ? "" : " (",
                          place.c_str(), place.empty() ? "" : ")");
            why = text;
            if (type) ok = true, weather = type;
        }
    }
    std::lock_guard<std::mutex> l(sh->lock);
    sh->busy = false, sh->fresh = true, sh->ok = ok, sh->weather = weather, sh->status = why;
}

// Script-thread state.
DWORD g_nextFetch = 0;          // GetTickCount of the next fetch (0: as soon as on)
DWORD g_clockAt = 0;            // last clock set
bool g_clockHeld = false;       // PAUSE_CLOCK is ours
std::string g_target;           // weather we hold ("" none)
bool g_weatherHeld = false;     // our persistent weather is applied
DWORD g_fadeDone = 0;           // when the fade into g_target ends
bool g_settled = false;         // SET_WEATHER_TYPE_PERSIST after the fade
DWORD g_watchAt = 0;

bool ScriptsOwnWorld() {
    return Call<BOOL>(gta::GET_MISSION_FLAG) || Call<BOOL>(gta::IS_CUTSCENE_ACTIVE) ||
           Call<BOOL>(gta::IS_PLAYER_SWITCH_IN_PROGRESS);
}

void ReleaseClock() {
    if (!g_clockHeld) return;
    Call<void>(gta::PAUSE_CLOCK, 0);
    g_clockHeld = false;
}

void ReleaseWeather() {
    if (!g_weatherHeld) return;
    Call<void>(gta::CLEAR_WEATHER_TYPE_PERSIST);
    Call<void>(gta::CLEAR_OVERRIDE_WEATHER);
    g_weatherHeld = false;
    util::Logf(g_log, "SkateV weather: GTA's own weather again");
}

void ApplyWeather(float fade) {
    if (fade <= 0.5f) Call<void>(gta::SET_WEATHER_TYPE_NOW_PERSIST, g_target.c_str());
    else Call<void>(gta::SET_WEATHER_TYPE_OVERTIME_PERSIST, g_target.c_str(), fade);
    g_weatherHeld = true, g_settled = false;
    g_fadeDone = GetTickCount() + static_cast<DWORD>(fade * 1000.0f) + 1000;
    util::Logf(g_log, "SkateV weather: fading to %s over %.0f s", g_target.c_str(), fade);
}
}  // namespace

void Start(LogFn log, std::function<void(const std::string&)> notify) { g_log = log, g_notify = std::move(notify); }

void Configure(const Options& o) {
    const bool locationChanged = o.location != g_opt.location;
    const bool weatherOn = o.weather && !g_opt.weather;
    g_opt = o;
    if (g_opt.refreshMinutes < 5) g_opt.refreshMinutes = 5;
    if (locationChanged || weatherOn) g_nextFetch = 0;
}

void Refresh() { g_nextFetch = 0; }

std::string Status() {
    std::lock_guard<std::mutex> l(g_shared->lock);
    if (g_shared->busy) return "Fetching the weather...";
    if (g_shared->status.empty()) return g_opt.weather ? "No weather fetched yet" : "Real-life weather is off";
    return g_shared->status + (g_shared->ok ? " -> " + g_shared->weather : std::string());
}

void Tick() {
    const DWORD now = GetTickCount();
    const bool scripts = (g_opt.time || g_opt.weather || g_clockHeld || g_weatherHeld) && ScriptsOwnWorld();

    // Clock: the PC's local time, once a second, GTA's own clock paused in between.
    if (!g_opt.time || scripts) {
        ReleaseClock();
    } else if (!g_clockHeld || now - g_clockAt >= 1000) {
        SYSTEMTIME t{};
        GetLocalTime(&t);
        Call<void>(gta::SET_CLOCK_TIME, static_cast<int>(t.wHour), static_cast<int>(t.wMinute), static_cast<int>(t.wSecond));
        if (!g_clockHeld) {
            Call<void>(gta::PAUSE_CLOCK, 1);
            util::Logf(g_log, "SkateV clock: GTA follows the PC clock (%02u:%02u)", t.wHour, t.wMinute);
        }
        g_clockHeld = true, g_clockAt = now;
    }

    if (!g_opt.weather) {
        ReleaseWeather();
        g_target.clear();
        return;
    }
    // Fetch on the worker thread when due.
    {
        std::lock_guard<std::mutex> l(g_shared->lock);
        if (!g_shared->busy && (g_nextFetch == 0 || static_cast<int>(now - g_nextFetch) >= 0)) {
            g_shared->busy = true;
            g_nextFetch = now + static_cast<DWORD>(g_opt.refreshMinutes) * 60000u;
            std::thread(Fetch, g_shared, g_opt.location).detach();
        }
        if (g_shared->fresh) {
            g_shared->fresh = false;
            util::Logf(g_log, "SkateV weather: %s", (g_shared->status + (g_shared->ok ? " -> " + g_shared->weather : "")).c_str());
            if (!g_shared->ok) {
                g_nextFetch = now + 5u * 60000u;  // offline or not found: GTA's weather stays, try again soon
                if (g_notify) g_notify("Real weather: " + g_shared->status);
            } else if (g_shared->weather != g_target) {
                g_target = g_shared->weather;
                if (g_notify) g_notify("Real weather: " + g_shared->status);
                if (!scripts) ApplyWeather(g_opt.fadeSeconds);
            }
        }
    }
    if (g_target.empty()) return;
    if (scripts) {
        ReleaseWeather();
        return;
    }
    if (!g_weatherHeld) {
        ApplyWeather(g_opt.fadeSeconds);  // after a mission or cutscene
        return;
    }
    if (static_cast<int>(now - g_fadeDone) < 0) return;
    if (!g_settled) {
        Call<void>(gta::SET_WEATHER_TYPE_PERSIST, g_target.c_str());
        g_settled = true;
    }
    // GTA's weather cycle moved on regardless: fade back.
    if (now - g_watchAt < 10000) return;
    g_watchAt = now;
    Hash from = 0, to = 0;
    float pct = 0;
    Call<void>(gta::GET_CURR_WEATHER_STATE, &from, &to, &pct);
    const Hash want = util::Joaat(g_target);
    if (to != want && from != want) {
        util::Logf(g_log, "SkateV weather: GTA's weather left %s (now %08X -> %08X %.2f)", g_target.c_str(),
                   static_cast<unsigned>(from), static_cast<unsigned>(to), pct);
        ApplyWeather(g_opt.fadeSeconds);
    }
}

}  // namespace realworld
